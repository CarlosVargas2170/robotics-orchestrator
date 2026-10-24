from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator"

    flussonic_url: str = "http://localhost:8080"
    flussonic_user: str = ""
    flussonic_password: str = ""

    # robotics-backend: emite los JWT y dice quien es el usuario (GET /api/auth/me)
    robotics_backend_url: str = ""
    auth_cache_seconds: float = 45.0

    clips_dir: Path = Path("./data/clips")
    # Un clip se borra al procesarlo, salvo que tenga una alerta confirmada (evidencia) o keep_clips=True
    keep_clips: bool = False
    clip_retention_days: int = 30

    # Worker: cuantos clips procesa a la vez, cada cuanto busca trabajo si no le avisan, y cuando se da por
    # muerto un clip que lleva tanto sin avanzar (se reintenta hasta max_attempts veces)
    worker_concurrency: int = 4
    worker_poll_interval_s: float = 2.0
    worker_shutdown_s: float = 60.0
    clip_stale_s: float = 600.0
    max_attempts: int = 3

    # Ingesta automatica: se pide solo video con al menos `ingest_lag_s` de antiguedad (el archivo ya cerrado)
    # y, tras una caida larga, no se recupera mas de `ingest_max_backlog_s` de atraso
    ingest_lag_s: float = 20.0
    ingest_max_backlog_s: float = 300.0

    frame_sample_fps: float = 1.0

    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
