"""CLI wrapper: run the crawl worker loop.

    python -m cestaplan_api.jobs.crawl_worker

Delegates to :func:`cestaplan_api.ingestion.crawl_worker.main` so the worker has a home
alongside the other job commands.
"""

from __future__ import annotations

from cestaplan_api.ingestion.crawl_worker import main

if __name__ == "__main__":
    main()
