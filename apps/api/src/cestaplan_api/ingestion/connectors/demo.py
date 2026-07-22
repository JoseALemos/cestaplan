"""Deterministic, network-free demo connector for the price-ingestion subsystem (FASE B).

:class:`DemoFixtureConnector` implements the full
:class:`~cestaplan_api.ingestion.contracts.RetailerConnector` contract against a bundled set
of **synthetic** fixtures for a fictional retailer, ``DemoFixtureMart``. Nothing here is
captured from, or ever talks to, a real website: the "raw" payloads a connector would fetch
and parse are hand-written Python data structures, clearly labelled synthetic. This makes the
whole ingestion vertical (fetch -> capture -> parse -> normalize -> validate -> anomaly ->
history -> coverage -> projection) runnable and testable end-to-end with zero network.

The connector is **deterministic** and **scenario-driven**: the same ``scenario`` always
yields the same catalogue and prices, so tests can assert exact behaviour. Scenarios model
both the happy path and the "do not trust this run" cases the pipeline must handle:

- :data:`SCENARIO_BASELINE`  -- the full ~26-product catalogue at its listed prices.
- :data:`SCENARIO_PRICE_CHANGE` -- one product's price moved (a legitimate change).
- :data:`SCENARIO_ANOMALY` -- one product spiked x100 (a per-variant anomaly to quarantine).
- :data:`SCENARIO_CATALOG_DROP` -- discovery collapses to 2 products (a batch anomaly).
- :data:`SCENARIO_BLOCK_PAGE` -- fetches return an anti-bot interstitial (never a price).

Money and physical quantities are :class:`decimal.Decimal`. The parse/normalize stages reuse
the shared :mod:`cestaplan_api.ingestion.normalization` helpers so the demo exercises exactly
the same code a real connector would.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.ingestion import (
    Capabilities,
    ConnectorStatus,
    FetchResult,
    HealthResult,
    LegalStatus,
    NormalizedObservation,
    ParseResult,
    PriceScope,
    PriceType,
    RetailerConnector,
    SourcePolicy,
    SourceRef,
    StoreResolutionResult,
    ValidationResult,
)
from cestaplan_api.ingestion.normalization import (
    ParsedProduct,
    PriceNormalizer,
    ProductNormalizer,
    PromotionParser,
)
from cestaplan_api.ingestion.validation import ObservationValidator, ValidationContext

# --------------------------------------------------------------------------- #
# Scenario identifiers (also accepted via a crawl job payload's ``scenario`` key)
# --------------------------------------------------------------------------- #
SCENARIO_BASELINE = "baseline"
SCENARIO_PRICE_CHANGE = "price_change"
SCENARIO_ANOMALY = "anomaly"
SCENARIO_CATALOG_DROP = "catalog_drop"
SCENARIO_BLOCK_PAGE = "block_page"

_SCENARIOS = frozenset(
    {
        SCENARIO_BASELINE,
        SCENARIO_PRICE_CHANGE,
        SCENARIO_ANOMALY,
        SCENARIO_CATALOG_DROP,
        SCENARIO_BLOCK_PAGE,
    }
)


# --------------------------------------------------------------------------- #
# Synthetic fixtures (NOT captured from any real site — hand-written test data)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DemoStoreFixture:
    """A single synthetic store the demo retailer is scoped to."""

    external_store_id: str
    name: str
    postal_code: str
    province: str
    locality: str


@dataclass(frozen=True, slots=True)
class DemoProductFixture:
    """A synthetic product listing as the demo retailer's "raw" source would expose it.

    ``variant_group`` links package-size siblings (e.g. 1 L and 5 L of the same oil) so the
    fixtures genuinely model variants; each fixture is still its own external product with an
    independent price history. ``promo_text`` is the free-text promotion label a real source
    would print (parsed by :class:`PromotionParser`), ``None`` when there is no offer.
    """

    external_id: str
    name: str
    brand: str
    category: str
    package_quantity: str
    package_unit: str
    package_count: int
    amount: str
    currency: str
    price_type: str
    promo_text: str | None
    available: bool
    variant_group: str


# Short aliases used only inside the fixture table below to keep lines readable.
_REG = PriceType.REGULAR.value
_PROMO = PriceType.PROMOTIONAL.value


#: The store the demo retailer is scoped to. Prices are ``exact_store`` scoped to it.
DEMO_STORE = DemoStoreFixture(
    external_store_id="DFM-STORE-001",
    name="DemoFixtureMart Centro (synthetic)",
    postal_code="28001",
    province="Madrid",
    locality="Madrid",
)

#: ~26 synthetic products spanning mass/volume/count units, multipacks and promotions.
DEMO_PRODUCTS: tuple[DemoProductFixture, ...] = (
    DemoProductFixture("DFM-0001", "Leche Entera DemoLact Brik 1 L", "DemoLact", "lacteos",
                       "1", "l", 1, "0.89", "EUR", _REG, None, True, "leche-entera"),
    DemoProductFixture("DFM-0002", "Leche Entera DemoLact Pack 6 x 1 L", "DemoLact", "lacteos",
                       "1", "l", 6, "5.10", "EUR", _REG, None, True, "leche-entera"),
    DemoProductFixture("DFM-0003", "Yogur Natural DemoLact 4 x 125 g", "DemoLact", "lacteos",
                       "125", "g", 4, "1.35", "EUR", _PROMO, "2x1", True, "yogur-natural"),
    DemoProductFixture("DFM-0004", "Queso Curado DemoLact Cuna 250 g", "DemoLact", "lacteos",
                       "250", "g", 1, "3.49", "EUR", _REG, None, True, "queso-curado"),
    DemoProductFixture("DFM-0005", "Aceite de Oliva Virgen Extra DemoOli 1 L", "DemoOli", "aceites",
                       "1", "l", 1, "6.95", "EUR", _REG, None, True, "aove"),
    DemoProductFixture("DFM-0006", "Aceite de Oliva Virgen Extra DemoOli 5 L", "DemoOli", "aceites",
                       "5", "l", 1, "32.50", "EUR", _PROMO,
                       "-30% de descuento", True, "aove"),
    DemoProductFixture("DFM-0007", "Arroz Redondo DemoGrano 1 kg", "DemoGrano", "despensa",
                       "1", "kg", 1, "1.15", "EUR", _REG, None, True, "arroz"),
    DemoProductFixture("DFM-0008", "Arroz Basmati DemoGrano 500 g", "DemoGrano", "despensa",
                       "500", "g", 1, "1.80", "EUR", _REG, None, True, "arroz-basmati"),
    DemoProductFixture("DFM-0009", "Pasta Macarrones DemoPasta 500 g", "DemoPasta", "despensa",
                       "500", "g", 1, "0.95", "EUR", _REG, None, True, "macarrones"),
    DemoProductFixture("DFM-0010", "Pasta Espaguetis DemoPasta 500 g", "DemoPasta", "despensa",
                       "500", "g", 1, "0.95", "EUR", _REG, None, True, "espaguetis"),
    DemoProductFixture("DFM-0011", "Tomate Triturado DemoHuerta 400 g", "DemoHuerta", "conservas",
                       "400", "g", 1, "0.72", "EUR", _REG, None, True, "tomate-triturado"),
    DemoProductFixture("DFM-0012", "Atun Claro DemoMar Pack 3 x 80 g", "DemoMar", "conservas",
                       "80", "g", 3, "2.85", "EUR", _PROMO,
                       "3x2", True, "atun-claro"),
    DemoProductFixture("DFM-0013", "Garbanzos Cocidos DemoHuerta 400 g", "DemoHuerta", "conservas",
                       "400", "g", 1, "0.79", "EUR", _REG, None, True, "garbanzos"),
    DemoProductFixture("DFM-0014", "Lentejas Cocidas DemoHuerta 400 g", "DemoHuerta", "conservas",
                       "400", "g", 1, "0.79", "EUR", _REG, None, True, "lentejas"),
    DemoProductFixture("DFM-0015", "Pan de Molde DemoHorno 460 g", "DemoHorno", "panaderia",
                       "460", "g", 1, "1.20", "EUR", _REG, None, True, "pan-molde"),
    DemoProductFixture("DFM-0016", "Huevos Camperos DemoAves Docena", "DemoAves", "frescos",
                       "12", "unit", 1, "2.45", "EUR", _REG, None, True, "huevos"),
    DemoProductFixture("DFM-0017", "Pechuga de Pollo DemoAves Bandeja 1 kg", "DemoAves", "frescos",
                       "1", "kg", 1, "5.90", "EUR", _REG, None, True, "pollo-pechuga"),
    DemoProductFixture("DFM-0018", "Manzana Golden DemoHuerta 1 kg", "DemoHuerta", "frescos",
                       "1", "kg", 1, "1.65", "EUR", _REG, None, True, "manzana"),
    DemoProductFixture("DFM-0019", "Platano de Canarias DemoHuerta 1 kg", "DemoHuerta", "frescos",
                       "1", "kg", 1, "1.99", "EUR", _PROMO,
                       "2a unidad al 50%", True, "platano"),
    DemoProductFixture("DFM-0020", "Agua Mineral DemoAgua Pack 6 x 1.5 L", "DemoAgua", "bebidas",
                       "1.5", "l", 6, "2.40", "EUR", _REG, None, True, "agua"),
    DemoProductFixture("DFM-0021", "Zumo de Naranja DemoZumo Brik 1 L", "DemoZumo", "bebidas",
                       "1", "l", 1, "1.45", "EUR", _REG, None, True, "zumo-naranja"),
    DemoProductFixture("DFM-0022", "Cafe Molido DemoCafe 250 g", "DemoCafe", "desayuno",
                       "250", "g", 1, "2.75", "EUR", _REG, None, True, "cafe-molido"),
    DemoProductFixture("DFM-0023", "Galletas Maria DemoHorno 800 g", "DemoHorno", "desayuno",
                       "800", "g", 1, "1.55", "EUR", _REG, None, True, "galletas"),
    DemoProductFixture("DFM-0024", "Detergente Liquido DemoLimpio 3 L", "DemoLimpio", "hogar",
                       "3", "l", 1, "6.20", "EUR", _REG, None, True, "detergente"),
    DemoProductFixture("DFM-0025", "Papel Higienico DemoLimpio 12 rollos", "DemoLimpio", "hogar",
                       "12", "unit", 1, "4.30", "EUR", _REG, None, False, "papel-higienico"),
    DemoProductFixture("DFM-0026", "Sal Fina DemoSal 1 kg", "DemoSal", "despensa",
                       "1", "kg", 1, "0.45", "EUR", _REG, None, True, "sal"),
)

#: A synthetic anti-bot interstitial body (clearly fake) used by the block-page scenario.
BLOCK_PAGE_BODY: bytes = (
    b"<html><head><title>Just a moment...</title></head><body>"
    b"<h1>Access denied</h1><p>Please enable JavaScript and cookies to continue. "
    b"Are you a robot? Complete the captcha.</p></body></html>"
)

# The single product whose price moves / spikes in the mutation scenarios.
_MUTATED_EXTERNAL_ID = "DFM-0001"
_PRICE_CHANGE_AMOUNT = "0.95"  # 0.89 -> 0.95: a legitimate ~7% change
_ANOMALY_AMOUNT = "89.00"  # 0.89 -> 89.00: an x100 slip the anomaly detector must catch
# The two products discovery still returns under the catalog-drop scenario.
_CATALOG_DROP_KEEP = ("DFM-0001", "DFM-0002")


# --------------------------------------------------------------------------- #
# Parsed intermediate (raw -> parsed record, before normalization)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DemoParsedRecord:
    """A parsed product record: raw fields extracted from a capture, not yet normalized."""

    external_id: str
    name: str
    brand: str
    package_quantity: str
    package_unit: str
    package_count: int
    amount: str
    currency: str
    price_type: str
    promo_text: str | None
    available: bool


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DemoFixtureConnector(RetailerConnector):
    """A fully-implemented, deterministic connector backed by synthetic fixtures.

    All data-access methods resolve against the module-level fixtures — no HTTP is ever
    performed. ``scenario`` selects the catalogue/price behaviour (see the module docstring).
    """

    retailer_code = "demofixturemart"
    connector_version = "1.0.0"
    parser_version = "1.0.0"

    def __init__(self, *, scenario: str = SCENARIO_BASELINE) -> None:
        if scenario not in _SCENARIOS:
            raise ValueError(f"unknown demo scenario: {scenario!r}")
        self.scenario = scenario
        self._product_normalizer = ProductNormalizer()
        self._price_normalizer = PriceNormalizer()
        self._promotion_parser = PromotionParser()
        self._validator = ObservationValidator()
        self._by_id = {p.external_id: p for p in DEMO_PRODUCTS}

    # -- required ------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        """The demo exposes a full, store-scoped catalogue with prices and promotions."""
        return Capabilities(
            full_catalog=True,
            prices=True,
            promotions=True,
            availability=True,
            exact_store_scope=True,
            # Honest about what the fixtures do NOT model:
            loyalty_prices=False,
            delivery_zone_scope=False,
            regional_scope=False,
            national_scope=False,
            product_images=False,
            barcodes=False,
            nutrition=False,
            incremental_sync=False,
        )

    def source_policy(self) -> SourcePolicy:
        """A demo source: public, robots-respecting, no real domains to talk to."""
        return SourcePolicy(
            allowed_domains=(),
            request_delay=0.0,
            max_concurrency=1,
            respects_robots=True,
            legal_status=LegalStatus.PUBLIC,
            contact="demo@cestaplan.example",
        )

    # -- health -------------------------------------------------------------- #
    def health_check(self) -> HealthResult:
        """Fixtures are always reachable; report a healthy, active connector."""
        return HealthResult(
            status=ConnectorStatus.ACTIVE,
            ok=True,
            supported=True,
            checked_at=datetime.now(UTC),
            latency_ms=0,
            detail=f"demo fixtures ready ({len(DEMO_PRODUCTS)} products)",
        )

    # -- store resolution & discovery ---------------------------------------- #
    def resolve_store(
        self,
        *,
        postal_code: str | None = None,
        store_id: str | None = None,
        external_store_id: str | None = None,
    ) -> StoreResolutionResult:
        """Resolve any request to the single synthetic store (exact-store scope)."""
        return StoreResolutionResult(
            ok=True,
            supported=True,
            resolved_retailer_code=self.retailer_code,
            resolved_store_ref=DEMO_STORE.external_store_id,
            external_store_id=DEMO_STORE.external_store_id,
            scope=PriceScope.EXACT_STORE,
            resolution_method="fixture",
            confidence=Decimal("1.0"),
            evidence={
                "postal_code": DEMO_STORE.postal_code,
                "province": DEMO_STORE.province,
                "locality": DEMO_STORE.locality,
                "name": DEMO_STORE.name,
            },
        )

    def discover_products(self, *, cursor: str | None = None) -> FetchResult:
        """Enumerate the product external ids visible under the active scenario."""
        ids = self._visible_ids()
        return FetchResult(ok=True, supported=True, status_code=200, payload=tuple(ids))

    def _visible_ids(self) -> tuple[str, ...]:
        if self.scenario == SCENARIO_CATALOG_DROP:
            return _CATALOG_DROP_KEEP
        return tuple(p.external_id for p in DEMO_PRODUCTS)

    # -- fetch --------------------------------------------------------------- #
    def fetch_product(self, external_id: str, **kwargs: object) -> FetchResult:
        """Return the raw fixture payload for ``external_id`` as a source would yield it."""
        product = self._by_id.get(external_id)
        if product is None:
            return FetchResult(ok=False, supported=True, status_code=404,
                               error=f"unknown external_id {external_id!r}")

        if self.scenario == SCENARIO_BLOCK_PAGE:
            return FetchResult(
                ok=False,
                supported=True,
                url=self._source_url(external_id),
                status_code=403,
                content=BLOCK_PAGE_BODY,
                content_type="text/html",
                body_hash=_sha256_hex(BLOCK_PAGE_BODY),
                is_block_page=True,
                error="block_page",
            )

        raw = self._raw_payload(product)
        body = json.dumps(raw, sort_keys=True).encode("utf-8")
        return FetchResult(
            ok=True,
            supported=True,
            url=self._source_url(external_id),
            status_code=200,
            content=body,
            content_type="application/json",
            body_hash=_sha256_hex(body),
            payload=raw,
        )

    def fetch_category(self, category: str, **kwargs: object) -> FetchResult:
        """Return the raw payloads of every visible product in ``category``."""
        if self.scenario == SCENARIO_BLOCK_PAGE:
            return FetchResult(
                ok=False, supported=True, status_code=403, content=BLOCK_PAGE_BODY,
                content_type="text/html", body_hash=_sha256_hex(BLOCK_PAGE_BODY),
                is_block_page=True, error="block_page",
            )
        visible = set(self._visible_ids())
        rows = [
            self._raw_payload(p)
            for p in DEMO_PRODUCTS
            if p.category == category and p.external_id in visible
        ]
        return FetchResult(ok=True, supported=True, status_code=200, payload=tuple(rows))

    def _source_url(self, external_id: str) -> str:
        return f"demo://{self.retailer_code}/{DEMO_STORE.external_store_id}/product/{external_id}"

    def _raw_payload(self, product: DemoProductFixture) -> dict[str, object]:
        """The scenario-adjusted "raw" record for a product (what a source would return)."""
        amount = product.amount
        price_type = product.price_type
        if product.external_id == _MUTATED_EXTERNAL_ID:
            if self.scenario == SCENARIO_PRICE_CHANGE:
                amount = _PRICE_CHANGE_AMOUNT
            elif self.scenario == SCENARIO_ANOMALY:
                amount = _ANOMALY_AMOUNT
        return {
            "external_id": product.external_id,
            "name": product.name,
            "brand": product.brand,
            "category": product.category,
            "package": {
                "quantity": product.package_quantity,
                "unit": product.package_unit,
                "count": product.package_count,
            },
            "price": {"amount": amount, "currency": product.currency, "type": price_type},
            "promotion": product.promo_text,
            "available": product.available,
        }

    # -- parse & normalize --------------------------------------------------- #
    def parse_product(self, capture: object, **kwargs: object) -> ParseResult:
        """Parse a raw capture into structured records, then normalize them.

        ``capture`` may be a :class:`FetchResult` (its ``payload`` is used) or the raw dict
        itself. A block-page capture yields no observations. Parsing extracts the raw fields
        into :class:`DemoParsedRecord`s and delegates to :meth:`normalize_product` so both
        pipeline stages run exactly as a real connector's would.
        """
        raw = capture.payload if isinstance(capture, FetchResult) else capture
        if isinstance(capture, FetchResult) and capture.is_block_page:
            return ParseResult(ok=False, supported=True,
                               warnings=("capture is a block page, not price data",),
                               error="block_page")
        records = self._parse_raw(raw)
        if not records:
            return ParseResult(ok=False, supported=True, error="no parseable records")
        return self.normalize_product(records)

    def _parse_raw(self, raw: object) -> list[DemoParsedRecord]:
        rows = raw if isinstance(raw, (list, tuple)) else [raw]
        records: list[DemoParsedRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            package = row.get("package") or {}
            price = row.get("price") or {}
            if not isinstance(package, dict) or not isinstance(price, dict):
                continue
            records.append(
                DemoParsedRecord(
                    external_id=str(row["external_id"]),
                    name=str(row["name"]),
                    brand=str(row.get("brand") or ""),
                    package_quantity=str(package.get("quantity")),
                    package_unit=str(package.get("unit")),
                    package_count=int(package.get("count") or 1),
                    amount=str(price.get("amount")),
                    currency=str(price.get("currency") or "EUR"),
                    price_type=str(price.get("type") or PriceType.REGULAR.value),
                    promo_text=(str(row["promotion"]) if row.get("promotion") else None),
                    available=bool(row.get("available", True)),
                )
            )
        return records

    def normalize_product(self, parsed: object, **kwargs: object) -> ParseResult:
        """Normalize parsed records into :class:`NormalizedObservation`s.

        Uses the shared :class:`ProductNormalizer` / :class:`PriceNormalizer` /
        :class:`PromotionParser` so units, unit prices and promotions are produced by the
        same code a real connector relies on. ``observed_at`` is taken from ``as_of`` when
        supplied (keyword), else the current time.
        """
        records = self._as_records(parsed)
        as_of = kwargs.get("as_of")
        observed_at = as_of if isinstance(as_of, datetime) else datetime.now(UTC)
        observations: list[NormalizedObservation] = []
        warnings: list[str] = []
        for rec in records:
            product = self._product_normalizer.normalize(
                ParsedProduct(
                    name=rec.name,
                    brand=rec.brand or None,
                    package_quantity=rec.package_quantity,
                    package_unit=rec.package_unit,
                    package_count=rec.package_count,
                )
            )
            price = self._price_normalizer.normalize(
                rec.amount,
                rec.currency,
                package_quantity=product.package_quantity,
                package_unit=product.package_unit,
                package_count=product.package_count,
            )
            if price.amount is None:
                warnings.append(f"{rec.external_id}: missing price amount")
                continue
            promotion = self._promotion_parser.parse(rec.promo_text, now=observed_at)
            observations.append(
                NormalizedObservation(
                    variant_ref=rec.external_id,
                    amount=price.amount,
                    currency=price.currency,
                    price_scope=PriceScope.EXACT_STORE,
                    price_type=PriceType(rec.price_type),
                    observed_at=observed_at,
                    unit_amount=price.unit_amount,
                    unit_code=price.unit_code,
                    promotion=promotion,
                    requires_loyalty=promotion.loyalty_required if promotion else False,
                    available=rec.available,
                    confidence=Decimal("1.0"),
                    source=SourceRef(
                        source_slug=self.retailer_code,
                        source_url=self._source_url(rec.external_id),
                        connector_version=self.connector_version,
                        parser_version=self.parser_version,
                    ),
                )
            )
        return ParseResult(ok=True, supported=True, observations=tuple(observations),
                           warnings=tuple(warnings))

    def _as_records(self, parsed: object) -> list[DemoParsedRecord]:
        if isinstance(parsed, DemoParsedRecord):
            return [parsed]
        if isinstance(parsed, ParseResult):  # already-normalized: nothing to redo
            return []
        if isinstance(parsed, (list, tuple)):
            out: list[DemoParsedRecord] = []
            for item in parsed:
                if isinstance(item, DemoParsedRecord):
                    out.append(item)
                elif isinstance(item, dict):
                    out.extend(self._parse_raw(item))
            return out
        if isinstance(parsed, dict):
            return self._parse_raw(parsed)
        return []

    # -- validate ------------------------------------------------------------ #
    def validate_observation(self, observation: NormalizedObservation) -> ValidationResult:
        """Connector-level validation: store-scoped fixtures always have a store link."""
        return self._validator.validate(
            observation,
            ValidationContext(has_store_link=True, known_currencies=frozenset({"EUR"})),
        )

    # -- capability flags ---------------------------------------------------- #
    def supports_incremental_sync(self) -> bool:
        return False


def package_of(external_id: str) -> tuple[str | None, str | None, int]:
    """Return ``(package_quantity, package_unit, package_count)`` for a fixture product.

    Lets the orchestration recompute unit-price coherence during validation without
    re-parsing the raw payload. Unknown ids yield ``(None, None, 1)``.
    """
    product = {p.external_id: p for p in DEMO_PRODUCTS}.get(external_id)
    if product is None:
        return None, None, 1
    return product.package_quantity, product.package_unit, product.package_count


def observations_for(
    connector: DemoFixtureConnector,
    external_ids: Iterable[str],
    *,
    as_of: datetime,
) -> Sequence[NormalizedObservation]:
    """Convenience: fetch -> parse -> normalize a set of products into observations.

    Skips block-page / unparseable fetches. Used by tests to assemble a batch quickly.
    """
    result: list[NormalizedObservation] = []
    for external_id in external_ids:
        fetched = connector.fetch_product(external_id)
        if not fetched.ok:
            continue
        parsed = connector.parse_product(fetched)
        if parsed.ok:
            result.extend(
                _restamp(obs, as_of) for obs in parsed.observations
            )
    return result


def _restamp(obs: NormalizedObservation, as_of: datetime) -> NormalizedObservation:
    from dataclasses import replace

    return replace(obs, observed_at=as_of)


__all__ = [
    "BLOCK_PAGE_BODY",
    "DEMO_PRODUCTS",
    "DEMO_STORE",
    "SCENARIO_ANOMALY",
    "SCENARIO_BASELINE",
    "SCENARIO_BLOCK_PAGE",
    "SCENARIO_CATALOG_DROP",
    "SCENARIO_PRICE_CHANGE",
    "DemoFixtureConnector",
    "DemoParsedRecord",
    "DemoProductFixture",
    "DemoStoreFixture",
    "observations_for",
    "package_of",
]
