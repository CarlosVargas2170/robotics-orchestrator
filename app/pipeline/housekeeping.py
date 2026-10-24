"""Limpieza de videos antiguos."""
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Clip

log = logging.getLogger(__name__)

async def cleanup_old_clips(session: AsyncSession, settings: Settings, now: datetime | None = None) -> int:
    """Borra del disco los videos que se conservaron (evidencia o fallos) pasados `clip_retention_days`.

    Los registros de clips y detecciones se mantienen; solo se libera el archivo (`path` queda en None).
    """
    now = now or datetime.now(UTC)
    old = (await session.execute(
        select(Clip).where(Clip.path.is_not(None), Clip.created_at < now - timedelta(days=settings.clip_retention_days))
    )).scalars().all()
    for clip in old:
        Path(clip.path).unlink(missing_ok=True)
        clip.path = None
    await session.commit()
    if old:
        log.info("limpieza: %d video(s) antiguos eliminados", len(old))
    return len(old)
