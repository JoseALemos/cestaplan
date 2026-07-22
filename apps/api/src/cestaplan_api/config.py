"""Application settings, loaded from environment / repo-root .env.

Nothing here is hardcoded that belongs in configuration — notably the OpenAI model
is read from the environment and never baked into business logic (see docs/OPENAI.md).
"""

from __future__ import annotations

import json
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
    # Configurable, per-model price table (JSON) used to impute UsageLedger.estimated_cost.
    # Format (prices per 1,000,000 tokens, as strings or numbers):
    #   {"gpt-5": {"input_per_million": "1.25", "output_per_million": "10.00"}}
    # Default EMPTY -> estimated_cost stays NULL (a cost is never fabricated).
    openai_price_table: str = ""

    # --- Commercial price feed (authorized_partner connector) ---
    # A GENERIC, config-driven connector to a paid third-party price API the operator
    # subscribes to (RadarSuper / Pepesto / …). Our code only CONSUMES an authorized API
    # with the operator's key; it does NOT scrape. DISABLED by default: empty base URL / key
    # means "unconfigured" and the connector refuses to run (see services/commercial_feed_sync).
    commercial_feed_base_url: str = ""
    commercial_feed_api_key: str = ""
    # Header used to send the key, "Name: Prefix" (prefix optional). Examples:
    #   "Authorization: Bearer"  ->  Authorization: Bearer <key>
    #   "x-api-key"              ->  x-api-key: <key>
    commercial_feed_auth_header: str = "Authorization: Bearer"
    # Endpoint path (appended to base URL) that lists priced products.
    commercial_feed_products_path: str = "/products"
    # Pagination style of the products endpoint: "none" | "page" | "offset".
    commercial_feed_pagination: Literal["none", "page", "offset"] = "none"
    commercial_feed_page_size: int = 100
    # Dotted path to the array of items inside the JSON response ("" = the response is the
    # array itself; common wrappers items/data/products/results are auto-detected).
    commercial_feed_items_path: str = ""
    # JSON object mapping CANONICAL field -> the provider's JSON field (dotted path allowed).
    # Canonical fields: barcode, product_ref, product_name, brand, amount, currency,
    #   unit_price, date, store_ref, category, promo_price, package_quantity, package_unit.
    # Default EMPTY -> the connector stays unconfigured (a record is never fabricated).
    #   e.g. {"barcode":"ean","product_name":"name","amount":"price","unit_price":"unit_price"}
    commercial_feed_mapping: str = ""
    # Human-readable source name / attribution stored on each price and the DataSource row.
    commercial_feed_source_name: str = "Feed comercial autorizado"
    commercial_feed_attribution: str = (
        "Precios cedidos por un proveedor comercial autorizado, licenciados por el operador."
    )
    commercial_feed_license_code: str = "proprietary"

    # --- Cloud metering / quotas (enforced only when deployment_mode == "cloud") ---
    # A value <= 0 disables that particular limit.
    cloud_monthly_generation_limit: int = 100
    cloud_daily_generation_limit: int = 0
    cloud_monthly_token_limit: int = 0

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

    @property
    def price_table(self) -> dict[str, dict[str, str]]:
        """Parsed ``openai_price_table``; ``{}`` (no cost imputed) when unset/invalid."""
        raw = self.openai_price_table.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        table: dict[str, dict[str, str]] = {}
        for model, prices in parsed.items():
            if isinstance(prices, dict):
                table[str(model)] = {str(k): str(v) for k, v in prices.items()}
        return table

    @property
    def commercial_feed_field_map(self) -> dict[str, str]:
        """Parsed ``commercial_feed_mapping`` (canonical field -> provider field).

        ``{}`` when unset/invalid — which keeps the connector unconfigured (a record is
        never fabricated from an empty mapping).
        """
        raw = self.commercial_feed_mapping.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in parsed.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()
        }

    @property
    def commercial_feed_configured(self) -> bool:
        """Whether the commercial feed has the minimum config to run (base URL + key + map)."""
        return bool(
            self.commercial_feed_base_url.strip()
            and self.commercial_feed_api_key.strip()
            and self.commercial_feed_field_map
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
