import uuid

from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.models import Clip
from app.db.session import SessionLocal
from app.detectors.registry import DetectorPool, detectors_for_camera
from app.ingest.flussonic import FlussonicClient
from app.pipeline.runner import process_clip

settings = get_settings()


async def startup(ctx: dict) -> None:
    setup_logging(settings.log_level)
    ctx["flussonic"] = FlussonicClient(settings)
    ctx["pool"] = DetectorPool()
    ctx["redis"] = Redis.from_url(settings.redis_url)


async def shutdown(ctx: dict) -> None:
    await ctx["flussonic"].aclose()
    await ctx["pool"].aclose()
    await ctx["redis"].aclose()


async def process_clip_job(ctx: dict, clip_id: str) -> None:
    async with SessionLocal() as session:
        clip = (
            await session.execute(
                select(Clip).options(selectinload(Clip.camera)).where(Clip.id == uuid.UUID(clip_id))
            )
        ).scalar_one()
        # Los servicios se leen de la BD en cada clip: un cambio de configuracion aplica al siguiente.
        rows = await detectors_for_camera(session, clip.camera)
        detectors = [ctx["pool"].get(r) for r in rows]
        await process_clip(session, clip, settings, ctx["flussonic"], detectors, ctx["redis"])


class WorkerSettings:
    functions = [process_clip_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = 3
