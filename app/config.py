from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value is overridable by an env var of the same name."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://certgen:certgen@localhost:5432/certgen"
    storage_dir: Path = Path("./storage")

    # A single request may carry many recipients; cap it so one caller cannot
    # exhaust memory or hold a transaction open indefinitely.
    max_recipients: int = 10_000

    # Worker tuning: rows claimed per round trip, and how long to idle when the queue is empty.
    worker_batch_size: int = 25
    worker_poll_seconds: float = 1.0

    # At-least-once delivery needs a retry cap and a visibility timeout, so a
    # worker that dies mid-render does not strand its rows in `processing`.
    max_attempts: int = 3
    visibility_timeout_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
