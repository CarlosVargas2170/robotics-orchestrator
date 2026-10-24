"""Eventos en vivo para Robot Control Interface sobre PostgreSQL (LISTEN/NOTIFY).

El worker publica cada deteccion y alerta con `pg_notify`; la API escucha una sola vez por proceso y
reparte a los WebSocket conectados. Es el punto a cambiar si algun dia se usa Redis: bastaria otra clase
con el mismo contrato (`EventPublisher.publish` en el worker, `EventBus` en la API).

Los eventos son efimeros (no se guardan): quien no esta conectado en ese momento no los recibe, y los datos
siempre se pueden consultar despues en GET /detections. LISTEN necesita una conexion directa a PostgreSQL,
no una detras de un pooler en modo transaccion (p. ej. pgbouncer o el pooler de Supabase).
"""
import asyncio
import logging
from typing import Protocol

import asyncpg

log = logging.getLogger(__name__)

CHANNEL = "detections"
MAX_PAYLOAD_BYTES = 7900  # NOTIFY admite 8000 bytes por mensaje


def asyncpg_dsn(database_url: str) -> str:
    """DATABASE_URL de SQLAlchemy (postgresql+asyncpg://...) -> DSN que entiende asyncpg."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class EventPublisher(Protocol):
    async def publish(self, message: str) -> None: ...


class PostgresEventPublisher:
    """Publica con su propia conexion en autocommit: el aviso sale al instante y no espera a que termine
    la transaccion larga de un clip (NOTIFY dentro de una transaccion solo se entrega al confirmarla)."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None

    async def publish(self, message: str) -> None:
        if len(message.encode()) > MAX_PAYLOAD_BYTES:
            log.warning("evento descartado: supera el limite de NOTIFY (%d bytes)", MAX_PAYLOAD_BYTES)
            return
        for _ in range(2):  # un reintento por si la conexion se cayo
            try:
                if self._conn is None or self._conn.is_closed():
                    self._conn = await asyncpg.connect(self._dsn)
                await self._conn.execute("SELECT pg_notify($1, $2)", CHANNEL, message)
                return
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
                await self._drop()
        log.warning("evento descartado: PostgreSQL no responde")

    async def _drop(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close(timeout=2)
            except Exception:  # noqa: BLE001 - la conexion ya estaba rota
                pass
            self._conn = None

    async def aclose(self) -> None:
        await self._drop()


class EventBus:
    """Escucha el canal y reparte cada evento a las colas de los WebSocket suscritos."""

    def __init__(self, dsn: str, reconnect_s: float = 5.0):
        self._dsn = dsn
        self._reconnect_s = reconnect_s
        self._conn: asyncpg.Connection | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._connect()
        self._task = asyncio.create_task(self._supervise())

    async def _connect(self) -> None:
        try:
            self._conn = await asyncpg.connect(self._dsn)
            await self._conn.add_listener(CHANNEL, self._on_notify)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            log.warning("EventBus sin conexion a PostgreSQL (%s); se reintenta", exc.__class__.__name__)
            self._conn = None

    async def _supervise(self) -> None:
        """Reconecta si PostgreSQL se reinicia o la conexion se corta."""
        while True:
            await asyncio.sleep(self._reconnect_s)
            if self._conn is None or self._conn.is_closed():
                await self._connect()

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # cliente demasiado lento: se pierde el evento, no se frena a los demas

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._conn is not None and not self._conn.is_closed():
            await self._conn.close(timeout=2)
