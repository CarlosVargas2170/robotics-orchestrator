"""Cola de trabajo sobre la tabla `clips` (PostgreSQL).

Un clip en estado PENDING es un trabajo en espera. Un worker lo toma con `FOR UPDATE SKIP LOCKED`
(varios workers a la vez nunca toman el mismo) y lo pasa a DOWNLOADING. Es el punto a cambiar si algun dia
se usa Redis: `notify_new_work` encolaria el trabajo y `claim_next` desapareceria.
"""
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

WAKE_CHANNEL = "clips_pending"

# Toma el clip pendiente mas antiguo, salvo que la MISMA camara tenga uno anterior sin terminar: los clips de
# una camara se procesan de uno en uno y en orden (el tracking del servicio de personas se rompe si le llegan
# frames de dos tramos a la vez). Un clip en proceso que lleva mas de :stale segundos sin avanzar se da por
# muerto y no bloquea. Los pendientes anteriores siempre bloquean: van a procesarse.
_CLAIM = text("""
UPDATE clips SET status = 'DOWNLOADING', attempts = attempts + 1, updated_at = now()
WHERE id = (
    SELECT c.id FROM clips c
    WHERE c.status = 'PENDING'
      AND NOT EXISTS (
          SELECT 1 FROM clips e
          WHERE e.camera_id = c.camera_id
            AND e.started_at < c.started_at
            AND (e.status = 'PENDING'
                 OR (e.status IN ('DOWNLOADING', 'PROCESSING')
                     AND e.updated_at > now() - make_interval(secs => :stale)))
      )
    ORDER BY c.started_at
    FOR UPDATE OF c SKIP LOCKED
    LIMIT 1
)
RETURNING id
""")

_FAIL_EXHAUSTED = text("""
UPDATE clips SET status = 'FAILED', error = 'Se agotaron los reintentos', updated_at = now()
WHERE status IN ('DOWNLOADING', 'PROCESSING')
  AND updated_at < now() - make_interval(secs => :stale)
  AND attempts >= :max_attempts
""")

_REQUEUE_STALE = text("""
UPDATE clips SET status = 'PENDING', updated_at = now()
WHERE status IN ('DOWNLOADING', 'PROCESSING')
  AND updated_at < now() - make_interval(secs => :stale)
  AND attempts < :max_attempts
""")


async def claim_next(session: AsyncSession, *, stale_s: float) -> uuid.UUID | None:
    """Reserva el siguiente clip que toca procesar, o None si no hay ninguno."""
    clip_id = (await session.execute(_CLAIM, {"stale": stale_s})).scalar_one_or_none()
    await session.commit()
    return clip_id


async def recover_stale(session: AsyncSession, *, stale_s: float, max_attempts: int) -> tuple[int, int]:
    """Clips que un worker dejo a medias (cayo o se reinicio): se reencolan, o fallan si ya agotaron intentos.

    Devuelve (reencolados, fallidos).
    """
    params = {"stale": stale_s, "max_attempts": max_attempts}
    failed = (await session.execute(_FAIL_EXHAUSTED, params)).rowcount
    requeued = (await session.execute(_REQUEUE_STALE, params)).rowcount
    await session.commit()
    return requeued, failed


async def notify_new_work(session: AsyncSession) -> None:
    """Avisa a los workers de que hay un clip nuevo, para que no esperen a su siguiente consulta periodica."""
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": WAKE_CHANNEL})
    await session.commit()
