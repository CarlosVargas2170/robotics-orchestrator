from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator"
    redis_url: str = "redis://localhost:6379/0"

    flussonic_url: str = "http://localhost:8080"
    flussonic_user: str = ""
    flussonic_password: str = ""

    clips_dir: Path = Path("./data/clips")

    frame_sample_fps: float = 1.0
    detector_timeout_s: float = 30.0

    detector_objects_url: str = ""
    detector_faces_url: str = ""
    detector_animals_url: str = ""

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
