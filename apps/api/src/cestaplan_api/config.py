"""Application settings, loaded from environment / repo-root .env.

Nothing here is hardcoded that belongs in configuration — notably the OpenAI model
is read from the environment and never baked into business logic (see docs/OPENAI.md).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root is four levels up from this file in the source tree
# (apps/api/src/cestaplan_api/config.py -> repo root). In a container the tree is
# shallower (/app/src/cestaplan_api/config.py) and there is no repo-root .env — env vars
# come from the environment — so fall back to the highest available parent instead of
# indexing past the end (which would crash at import time).
_CONFIG_PARENTS = Path(__file__).resolve().parents
_REPO_ROOT = _CONFIG_PARENTS[4] if len(_CONFIG_PARENTS) > 4 else _CONFIG_PARENTS[-1]


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

    @field_validator("database_url")
    @classmethod
    def _pin_psycopg_driver(cls, value: str) -> str:
        """Force the psycopg (v3) driver onto bare Postgres URLs.

        Managed hosts (Railway, Render, Heroku, Fly) hand out ``postgres://`` or
        ``postgresql://`` URLs. SQLAlchemy maps those to the legacy ``psycopg2`` dialect,
        which is not installed — only ``psycopg`` v3 is. Rewrite the scheme so the same
        URL works whether it comes from a managed provider or the local default.
        """
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

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

    # --- Scraping / price-ingestion HTTP layer (FASE A, spec §23) ---
    # DISABLED by default: nothing fetches a public source unless the operator opts in.
    # None of these are secrets. The connectors additionally require their own enable flag
    # (below) AND the relevant DataSource row to be enabled before anything runs. We only
    # ever access sources on their declared legal footing and NEVER evade blocks/CAPTCHA/auth.
    scraping_enabled: bool = False
    price_sync_enabled: bool = False
    # Honest, identifiable User-Agent + optional operator/abuse contact (From header).
    scraping_user_agent: str = "CestaPlanBot/0.0 (+https://github.com/; price-ingestion)"
    scraping_contact_email: str = ""
    # Per-domain politeness: in-flight requests and the delay window between requests.
    scraping_max_concurrency: int = 2
    scraping_request_delay_min_ms: int = 500
    scraping_request_delay_max_ms: int = 1500
    scraping_timeout_seconds: int = 20
    scraping_max_retries: int = 3
    # Hard cap on a single response body; oversized downloads are aborted.
    scraping_max_response_mb: int = 5
    # Raw-capture retention (days) used to compute RawCapture.expires_at.
    raw_capture_retention_days: int = 30
    # Freshness thresholds for a stored price (hours) — used by coverage/health downstream.
    stale_price_hours: int = 24
    expired_price_hours: int = 48
    # Circuit breaker: open a domain after N consecutive failures, for this many minutes.
    connector_failure_threshold: int = 5
    connector_circuit_open_minutes: int = 30

    # --- Per-connector enable flags (all OFF by default; opt-in per retailer) ---
    mercadona_connector_enabled: bool = False
    alcampo_connector_enabled: bool = False
    carrefour_connector_enabled: bool = False
    dia_connector_enabled: bool = False
    lidl_offers_connector_enabled: bool = False
    aldi_offers_connector_enabled: bool = False
    deza_connector_enabled: bool = False

    # --- External price providers (FASE 1+, spec §17). All OFF by default; secrets empty.
    # No provider is an official retailer API; see docs/PRICE_PROVIDERS.md. ---
    price_providers_enabled: bool = False
    price_stale_hours: int = 48
    price_expired_hours: int = 168
    price_sync_max_concurrency: int = 2
    # Safeguards (FASE 2A, spec §O/§R/§S). A threshold <= 0 disables that particular check.
    price_provider_kill_switch: bool = False  # §S: hard stop for all external providers
    provider_require_rights_approval: bool = True  # §O: block prod sync without rights approval
    price_provider_max_daily_cost_eur: float = 0.0  # §S: 0 = per-provider caps only
    price_provider_max_daily_requests: int = 0
    price_provider_max_products_per_run: int = 1000
    price_provider_max_execution_seconds: int = 900
    price_provider_max_retries: int = 3
    # §R quality floors (ratios 0..1) below which a provider is not a main catalogue.
    provider_min_price_coverage: float = 0.95
    provider_min_package_coverage: float = 0.80
    provider_min_barcode_coverage: float = 0.0
    provider_min_observed_at_coverage: float = 0.95
    provider_max_catalog_drop_ratio: float = 0.50  # anomalous drop vs previous sync
    # Parse.bot (DIA / Alcampo) — X-API-Key, base URLs configurable.
    parse_bot_enabled: bool = False
    parse_bot_api_key: str = ""
    parse_bot_timeout_seconds: float = 30.0
    parse_bot_max_retries: int = 3
    parse_bot_dia_enabled: bool = False
    parse_bot_alcampo_enabled: bool = False
    parse_bot_dia_base_url: str = ""
    parse_bot_alcampo_base_url: str = ""
    # Apify (Mercadona actor) — Bearer token, actor id configurable, cost/quota caps.
    apify_enabled: bool = False
    apify_api_token: str = ""
    apify_base_url: str = "https://api.apify.com/v2"
    apify_mercadona_enabled: bool = False
    apify_mercadona_actor_id: str = "studio-amba~mercadona-scraper"
    apify_mercadona_default_postal_code: str = ""
    apify_max_wait_seconds: int = 900
    apify_poll_interval_seconds: float = 10.0
    apify_max_results_per_run: int = 1000
    apify_max_daily_runs: int = 5
    apify_max_daily_cost_eur: float = 10.0
    # Open Prices (Open Food Facts) — observations/tickets/cross-validation only.
    open_prices_enabled: bool = False
    open_prices_base_url: str = "https://prices.openfoodfacts.org/api/v1"

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
    def scraping_request_delay_bounds_seconds(self) -> tuple[float, float]:
        """(min, max) per-domain request delay in seconds, always ``min <= max``."""
        lo = max(0, self.scraping_request_delay_min_ms)
        hi = max(lo, self.scraping_request_delay_max_ms)
        return (lo / 1000.0, hi / 1000.0)

    @property
    def scraping_max_response_bytes(self) -> int:
        """Response-body hard cap in bytes, derived from ``scraping_max_response_mb``."""
        return max(1, self.scraping_max_response_mb) * 1024 * 1024

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
