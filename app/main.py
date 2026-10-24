from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import cameras, clips, detections, events, health
from app.auth.backend import close_auth_client
from app.core.config import get_settings
from app.core.logging import setup_logging

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    app.state.queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await app.state.queue.aclose()
    await close_auth_client()


app = FastAPI(title="Robotics Orchestrator", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (health.router, cameras.router, clips.router, detections.router, events.router):
    app.include_router(r)
