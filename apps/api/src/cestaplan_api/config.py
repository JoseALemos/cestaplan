"""Application settings, loaded from environment / repo-root .env.

Nothing here is hardcoded that belongs in configuration — notably the OpenAI model
is read from the environment and never baked into business logic (see docs/OPENAI.md).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root is four levels up from this file:
# apps/api/src/cestaplan_api/config.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Deployment / AI mode ---
    deployment_mode: Literal["self_hosted", "cloud"] = "self_hosted"
    ai_billing_mode: Literal["platform", "byok", "disabled"] = "disabled"

    # --- Database ---
    database_url: str = "postgresql+psycopg://cestaplan:cestaplan@localhost:5432/cestaplan"

    # --- API / web ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_public_url: str = "http://localhost:8000"
    web_public_url: str = "http://localhost:3000"
    cors_allowed_origins: str = "http://localhost:3000"

    # --- Auth / sessions ---
    session_secret: str = "dev-only-insecure-secret-change-me"
    session_ttl_hours: int = 720
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    # --- OpenAI (only used when ai_billing_mode != disabled) ---
    openai_api_key: str = ""
    openai_model: str = ""
    openai_reasoning_effort: str = "medium"
    openai_timeout_seconds: int = 60
    openai_max_retries: int = 2

    # --- Worker / job queue ---
    worker_poll_interval_seconds: float = 2.0
    worker_job_max_attempts: int = 3
    worker_heartbeat_seconds: int = 15

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def ai_enabled(self) -> bool:
        return self.ai_billing_mode != "disabled" and bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
