from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import alerts, cameras, clips, detections, detectors, events, health
from app.auth.backend import close_auth_client
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.events import EventBus, PostgresEventPublisher, asyncpg_dsn

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    app.state.events = EventBus(asyncpg_dsn(settings.database_url))
    await app.state.events.start()
    app.state.publisher = PostgresEventPublisher(asyncpg_dsn(settings.database_url))
    yield
    await app.state.events.stop()
    await app.state.publisher.aclose()
    await close_auth_client()


app = FastAPI(title="Robotics Orchestrator", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    health.router, cameras.router, detectors.router, clips.router, detections.router, alerts.router, events.router
):
    app.include_router(r)
