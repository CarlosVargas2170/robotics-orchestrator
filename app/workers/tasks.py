from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.models import Clip
from app.db.session import SessionLocal
from app.detectors.registry import build_detectors
from app.ingest.flussonic import FlussonicClient
from app.pipeline.runner import process_clip

settings = get_settings()


async def startup(ctx: dict) -> None:
    setup_logging(settings.log_level)
    ctx["flussonic"] = FlussonicClient(settings)
    ctx["detectors"] = build_detectors(settings)
    ctx["redis"] = Redis.from_url(settings.redis_url)


async def shutdown(ctx: dict) -> None:
    await ctx["flussonic"].aclose()
    await ctx["redis"].aclose()


async def process_clip_job(ctx: dict, clip_id: int) -> None:
    async with SessionLocal() as session:
        clip = (
            await session.execute(
                select(Clip).options(selectinload(Clip.camera)).where(Clip.id == clip_id)
            )
        ).scalar_one()
        await process_clip(session, clip, settings, ctx["flussonic"], ctx["detectors"], ctx["redis"])


class WorkerSettings:
    functions = [process_clip_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = 3
