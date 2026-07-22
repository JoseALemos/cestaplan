# FASE D — Connector reuse proof: `CsvFeedConnector`

## What this phase proves

FASE C added the first *real* connector, `OpenPricesConnector` (a paginated, crowdsourced HTTP
API — Open Food Facts Open Prices, ODbL). FASE D adds a **second real connector**,
`CsvFeedConnector`, whose only job is to prove the FASE A/B ingestion architecture is **reusable
across a structurally different source shape with zero coupling**.

`CsvFeedConnector` ingests an operator-provided **batch price feed** — a CSV or JSON document the
operator legitimately supplies (their own catalogue, a licensed feed, tickets). It is not a
scraper: a price is only ever what a feed row carries, absence is never turned into `0`, and
promotions are never fabricated. It flows through the **same** pipeline as every other connector:

```
discover -> fetch -> parse -> normalize -> validate -> anomaly-check ->
PriceObservation (append-only) -> coverage snapshot -> ProductPrice projection
```

reusing all the FASE A/B infrastructure unchanged (`normalization.py`, `validation.py`,
`anomaly.py`, `orchestration.run_price_sync`, `price_history.record_observation`, coverage and
current-price projection). It also **reuses the section-20 import column contract**: the feed is
parsed with the existing `CsvRetailerAdapter` / `JsonRetailerAdapter` (and
`adapters.base.CANONICAL_COLUMNS`), so the column shape (`retailer_slug`, `store_external_code`,
`product_external_id`, `amount`, `promotion`, `canonical_name`, …) is imported, not re-declared.

## Differences vs `OpenPricesConnector`

Both are real `RetailerConnector`s on the identical pipeline; only the *source-facing* half
differs. That the differing half is fully contained inside the connector is the whole point.

| Dimension          | `OpenPricesConnector` (FASE C)                     | `CsvFeedConnector` (FASE D)                                        |
| ------------------ | ------------------------------------------------- | ----------------------------------------------------------------- |
| Source shape       | Paginated crowdsourced HTTP API                   | Batch CSV/JSON feed (string, file path, or one URL)               |
| Transport          | `httpx` via `OpenPricesAdapter` (real network)    | In-memory parse of operator content; network only if a feed URL   |
| Legal footing      | `LegalStatus.PUBLIC` (ODbL open dataset)          | `LegalStatus.AUTHORIZED` (operator-provided / licensed)           |
| Store resolution   | OSM location (`osm:{TYPE}/{id}`) → always exact    | Feed-carried `store_external_code`; store-less rows → `national`  |
| Scope emitted      | Always `exact_store` (every price has an OSM loc.) | `exact_store` per row *iff* it carries a store, else `national`   |
| Promotions         | None — `promotions=False` (never fabricated)      | Parsed from a `promotion` column when present (`2x1` etc.)        |
| Price type         | Always `regular`                                  | `regular` (or `manual` default) unless a promo/loyalty overrides  |
| Discovery          | Barcodes returned by the API for one store        | `product_external_id` (or `barcode`) of each feed row             |
| Capabilities       | Fixed (crowdsourced, sparse, barcoded)            | **Computed from the actual feed** (promotions/barcodes/scope)     |
| Enable gate        | Open Prices `DataSource.is_enabled`               | The feed's own `DataSource.is_enabled` (by slug)                  |
| Confidence         | `0.5000` (open dataset)                           | `0.9500` authorized / `0.6500` otherwise                          |

Both share the honesty invariants: `full_catalog=False` / `partial_catalog=True`, missing prices
skipped (never `0`), no fabricated identifiers or promotions, and `exact_store` is never claimed
without a real store link (enforced by `ObservationValidator`).

## The decoupling proof — what did NOT change

Adding a second real connector against a completely different source shape required **no change
to any pipeline stage**. The only additions were:

1. one new file — `ingestion/connectors/feed.py` (`CsvFeedConnector`, a `RetailerConnector`); and
2. two lines in `ingestion/connectors/registry.py` — a `register_connector(...)` call plus a
   `build_csv_feed_connector(...)` builder gated by `DataSource.is_enabled`.

Explicitly **unchanged** (imported only, never edited):

- `orchestration.py` — `run_price_sync` / `run_crawl_job` drove the new connector as-is.
- `price_history.py` — append-only recording, change-closes-and-appends, quarantine.
- `anomaly.py` — the x100 price slip on a feed was quarantined by the same detector; last-good
  was left open and untouched.
- `validation.py`, `normalization.py` — reused for Decimal money, €/kg·€/l·€/unit unit prices,
  promotion parsing and the `exact_store`-needs-a-store-link rule.
- `coverage.py`, `current_price.py` — honest coverage snapshot + `ProductPrice` projection.
- `crawl_worker.py`, `queue.py`, `scheduler.py` — the queue/worker/scheduler were untouched.
- `contracts.py`, `ingestion/__init__.py` — the connector contract itself needed no new surface.
- No new Alembic migration (`alembic upgrade head` is clean).

Because the source-specific logic (transport, column mapping, scope/promo derivation, legal
footing) lives entirely behind the `RetailerConnector` interface, the pipeline neither knew nor
cared that this source is a batch feed rather than an HTTP API. That is the reusability /
no-coupling result FASE D set out to demonstrate.

## Tests

`tests/ingestion/test_feed_connector.py` (synthetic CSV/JSON fixtures, no network):

- contract honesty — capabilities computed from the feed, `AUTHORIZED` policy, health for
  enabled/disabled/unparseable feeds, discovery, exact-store vs national scope, `2x1` parsed and
  not collapsed, `€/kg`·`€/l` unit prices, missing-price and non-EUR rows skipped, JSON feed;
- full vertical via `run_price_sync` — append-only observations, real price change closes +
  appends, an x100 anomalous row quarantined with last-good untouched, honest coverage snapshot,
  `ProductPrice` projection, missing price never fabricated;
- registry exposure and the `DataSource.is_enabled` gate (enabled/disabled/absent source).
