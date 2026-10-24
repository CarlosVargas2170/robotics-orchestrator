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
    redis_url: str = "redis://localhost:6379/0"

    flussonic_url: str = "http://localhost:8080"
    flussonic_user: str = ""
    flussonic_password: str = ""

    # robotics-backend: emite los JWT y dice quien es el usuario (GET /api/auth/me)
    robotics_backend_url: str = ""
    auth_cache_seconds: float = 45.0

    clips_dir: Path = Path("./data/clips")

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
