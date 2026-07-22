"""Retailer connectors for the price-ingestion subsystem.

A connector implements the :class:`~cestaplan_api.ingestion.contracts.RetailerConnector`
contract for one retailer. :class:`DemoFixtureConnector` is a synthetic, network-free
reference implementation used by the end-to-end vertical; the ``registry`` module maps
retailer codes to connectors and provides the crawl worker's dispatch hook.
"""

from __future__ import annotations

from cestaplan_api.ingestion.connectors.demo import DemoFixtureConnector
from cestaplan_api.ingestion.connectors.registry import (
    build_worker_registry,
    get_connector,
    register_connector,
)

__all__ = [
    "DemoFixtureConnector",
    "build_worker_registry",
    "get_connector",
    "register_connector",
]
