"""Ingesta automatica: decide que tramo de video pedir a Flussonic para cada camara y lo encola.

Cada camara con `auto_ingest` recibe un tramo de `ingest_duration_s` segundos cada `ingest_interval_s`:
- interval == duration: cobertura continua (los tramos van pegados).
- interval > duration: muestreo (p. ej. 15 s de cada minuto), mas barato pero se pierde lo que ocurre entre medias.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Camera, Clip, ClipSource
from app.detectors.registry import detectors_for_camera
from app.queue import notify_new_work

log = logging.getLogger(__name__)


def next_window(
    last_start: datetime | None,
    now: datetime,
    *,
    interval_s: int,
    duration_s: int,
    lag_s: float,
    max_backlog_s: float,
) -> datetime | None:
    """Inicio del proximo tramo a pedir, o None si todavia no toca.

    Solo se piden tramos completos y con `lag_s` de antiguedad, para que Flussonic ya los tenga cerrados.
    """
    latest_start = (now - timedelta(seconds=lag_s + duration_s)).replace(microsecond=0)
    if last_start is None:
        return latest_start  # primera vez: solo el tramo mas reciente, sin recuperar historial

    start = last_start + timedelta(seconds=interval_s)
    if start > latest_start:
        return None
    if latest_start - start > timedelta(seconds=max_backlog_s):
        return latest_start  # estuvo parado mucho tiempo: se salta el atraso en vez de pedirlo todo de golpe
    return start


async def ingest_cameras(session: AsyncSession, now: datetime, settings: Settings) -> int:
    """Crea los clips que toquen (quedan PENDING en la cola) y avisa a los workers. Devuelve cuantos se crearon."""
    cameras = (await session.execute(
        select(Camera).where(Camera.enabled.is_(True), Camera.auto_ingest.is_(True))
    )).scalars().all()

    created: list = []
    for camera in cameras:
        start = next_window(
            camera.last_ingest_start, now,
            interval_s=camera.ingest_interval_s, duration_s=camera.ingest_duration_s,
            lag_s=settings.ingest_lag_s, max_backlog_s=settings.ingest_max_backlog_s,
        )
        if start is None:
            continue
        camera.last_ingest_start = start  # se avanza aunque no haya nada que pedir, para no acumular atraso
        if not await detectors_for_camera(session, camera):
            continue  # sin servicios activos no hay nada que analizar: no se descarga video

        clip_id = (await session.execute(
            insert(Clip)
            .values(camera_id=camera.id, started_at=start, duration_s=camera.ingest_duration_s,
                    source=ClipSource.SCHEDULE)
            .on_conflict_do_nothing(
                index_elements=["camera_id", "started_at"], index_where=text("source = 'SCHEDULE'")
            )
            .returning(Clip.id)
        )).scalar_one_or_none()
        if clip_id is not None:
            created.append(clip_id)

    await session.commit()
    if created:
        await notify_new_work(session)
        log.info("ingesta: %d clip(s) en cola", len(created))
    return len(created)
