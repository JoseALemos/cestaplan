"""Runnable CLI job commands for the price-ingestion subsystem (FASE A).

Each module exposes a ``main()`` and is runnable outside Railway as a plain CLI::

    python -m cestaplan_api.jobs.schedule_daily_price_sync
    python -m cestaplan_api.jobs.sync_retailer --retailer mercadona
    python -m cestaplan_api.jobs.sync_store --store-id <uuid>
    python -m cestaplan_api.jobs.retry_failed --run-id <uuid>
    python -m cestaplan_api.jobs.reprocess_capture --capture-id <uuid>
    python -m cestaplan_api.jobs.connector_health
    python -m cestaplan_api.jobs.crawl_worker
"""

from __future__ import annotations

__all__: list[str] = []
