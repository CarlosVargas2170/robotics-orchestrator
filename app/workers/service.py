"""Worker: toma clips de la cola (tabla clips), los procesa y ejecuta las tareas periodicas.

Se ejecuta con `python -m app.workers`. Se pueden lanzar varios procesos: la cola reparte el trabajo sin
duplicarlo y las tareas periodicas son idempotentes.
"""
import asyncio
import logging
import uuid
from datetime import UTC, datetime

import asyncpg
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.logging import setup_logging
from app.db.models import Clip
from app.db.session import SessionLocal
from app.detectors.registry import DetectorPool, detectors_for_camera
from app.events import PostgresEventPublisher, asyncpg_dsn
from app.ingest.flussonic import FlussonicClient
from app.ingest.scheduler import ingest_cameras
from app.pipeline.housekeeping import cleanup_old_clips
from app.pipeline.runner import process_clip
from app.queue import WAKE_CHANNEL, claim_next, recover_stale

log = logging.getLogger(__name__)

INGEST_EVERY_S = 30
RECOVER_EVERY_S = 60
CLEANUP_HOUR = 3  # hora local del servidor


class Worker:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def stop(self) -> None:
        """Pide un apagado ordenado: se termina el clip en curso y no se toman mas."""
        self._stop.set()

    async def run(self) -> None:
        s = self._settings
        setup_logging(s.log_level)
        self._flussonic = FlussonicClient(s)
        self._pool = DetectorPool()
        self._events = PostgresEventPublisher(asyncpg_dsn(s.database_url))
        listener = await self._listen_for_work()

        consumers = [asyncio.create_task(self._consume(n)) for n in range(s.worker_concurrency)]
        periodic = [
            asyncio.create_task(self._every(INGEST_EVERY_S, self._ingest, aligned=True)),
            asyncio.create_task(self._every(RECOVER_EVERY_S, self._recover)),
            asyncio.create_task(self._every(RECOVER_EVERY_S, self._cleanup_nightly)),
        ]
        log.info("worker listo: %d consumidores", s.worker_concurrency)

        await self._stop.wait()
        log.info("apagando: se espera a los clips en curso (hasta %.0f s)", s.worker_shutdown_s)
        for task in periodic:
            task.cancel()
        _, pending = await asyncio.wait(consumers, timeout=s.worker_shutdown_s)
        for task in pending:  # el clip queda DOWNLOADING/PROCESSING y se reencola solo al vencer clip_stale_s
            task.cancel()
        await asyncio.gather(*periodic, *consumers, return_exceptions=True)

        if listener is not None and not listener.is_closed():
            await listener.close(timeout=2)
        await self._events.aclose()
        await self._pool.aclose()
        await self._flussonic.aclose()

    async def _listen_for_work(self) -> asyncpg.Connection | None:
        """Despierta a los consumidores al instante cuando llega un clip. Es solo una optimizacion: sin ella
        igual se consulta cada `worker_poll_interval_s`."""
        try:
            conn = await asyncpg.connect(asyncpg_dsn(self._settings.database_url))
            await conn.add_listener(WAKE_CHANNEL, lambda *_: self._wake.set())
            return conn
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("sin aviso inmediato de clips nuevos (%s); se consulta periodicamente", exc.__class__.__name__)
            return None

    # --- Procesamiento ----------------------------------------------------------------------

    async def _consume(self, n: int) -> None:
        s = self._settings
        while not self._stop.is_set():
            try:
                self._wake.clear()
                async with SessionLocal() as session:
                    clip_id = await claim_next(session, stale_s=s.clip_stale_s)
                if clip_id is None:
                    try:
                        await asyncio.wait_for(self._wake.wait(), s.worker_poll_interval_s)
                    except TimeoutError:
                        pass
                    continue
                await self._process(clip_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("consumidor %d: error inesperado", n)
                await asyncio.sleep(1)

    async def _process(self, clip_id: uuid.UUID) -> None:
        async with SessionLocal() as session:
            clip = (await session.execute(
                select(Clip).options(selectinload(Clip.camera)).where(Clip.id == clip_id)
            )).scalar_one()
            # Los servicios se leen de la BD en cada clip: un cambio de configuracion aplica al siguiente
            rows = await detectors_for_camera(session, clip.camera)
            detectors = [self._pool.get(r) for r in rows]
            await process_clip(session, clip, self._settings, self._flussonic, detectors, self._events)

    # --- Tareas periodicas ------------------------------------------------------------------

    async def _every(self, seconds: int, job, *, aligned: bool = False) -> None:
        """Ejecuta `job` cada `seconds` (alineado al reloj si `aligned`: :00 y :30 para 30 s)."""
        while True:
            now = datetime.now(UTC)
            delay = seconds - (now.timestamp() % seconds) if aligned else seconds
            await asyncio.sleep(delay)
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tarea periodica %s fallo", job.__name__)

    async def _ingest(self) -> None:
        # Varios workers pueden coincidir: lo evitan el avance de last_ingest_start y el indice unico de clips
        async with SessionLocal() as session:
            await ingest_cameras(session, datetime.now(UTC), self._settings)

    async def _recover(self) -> None:
        async with SessionLocal() as session:
            requeued, failed = await recover_stale(
                session, stale_s=self._settings.clip_stale_s, max_attempts=self._settings.max_attempts
            )
        if requeued or failed:
            log.warning("recuperacion: %d clip(s) reencolados, %d fallidos por agotar intentos", requeued, failed)

    async def _cleanup_nightly(self) -> None:
        if datetime.now().hour != CLEANUP_HOUR or getattr(self, "_cleaned_on", None) == datetime.now().date():
            return
        self._cleaned_on = datetime.now().date()
        async with SessionLocal() as session:
            await cleanup_old_clips(session, self._settings)
