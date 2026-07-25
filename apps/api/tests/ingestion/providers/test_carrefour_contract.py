"""Carrefour semantic contract (spec §6): raw fingerprint may vary between captures, but the
semantic contract stays stable while the MEANING is unchanged. Synthetic fixtures only — never a
versioned real capture. The mapper gates on the semantic contract, not the raw fingerprint."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.ingestion.providers.parsebot import carrefour_contract as cf
from cestaplan_api.ingestion.providers.parsebot.chains import (
    ParseBotAlcampoMapper,
    ParseBotCarrefourMapper,
    UnsupportedSchemaError,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
STABLE_FP = cf.semantic_contract_fingerprint()


def _rec(**over: object) -> dict:
    """A minimally-valid synthetic Carrefour record (flat scalars). Override to build variants."""
    base: dict[str, object] = {
        "product_id": "CF-1",
        "name": "Leche entera 1L",
        "brand": "Carrefour",
        "regular_price": "1.19",
        "promotional_price": None,
        "loyalty_price": None,
        "net_content": "1L",
        "package_quantity": 1,
        "package_unit": "l",
        "unit_price": "1.19",
        "unit_price_unit": "per_litre",
        "availability": "in_stock",
        "ean": None,
        "postal_code": "28001",
        "sale_point": "0123",
        "observed_at": None,
        "promotion_text": None,
        "promotion_start_date": None,
        "promotion_end_date": None,
        "category": "Lacteos",
    }
    base.update(over)
    return base


# --- §6.1-4: compatible variants share the SAME stable semantic contract ------------------- #
def test_variants_1_to_4_share_stable_semantic_contract() -> None:
    no_extras = [_rec()]  # 1: no promo/ean/loyalty
    with_extras = [_rec(promotional_price="0.99", ean="8410000000017", loyalty_price="0.95")]  # 2
    mixed = [_rec(), _rec(product_id="CF-2", promotional_price="0.89", ean="8410000000024")]  # 3
    nulls = [_rec(ean=None, promotional_price=None, loyalty_price=None)]  # 4: optionals as null

    for batch in (no_extras, with_extras, mixed, nulls):
        r = cf.validate_semantic_contract(batch)
        assert r.processable is True
        assert r.contract_fingerprint == STABLE_FP  # stable identity, whatever the optionals

    # The RAW fingerprints DO differ (that is exactly why raw pinning was fragile)...
    m = ParseBotCarrefourMapper()
    assert m.detect_schema(no_extras) != m.detect_schema(with_extras)


# --- §6.5: an optional with an unexpected type -> review_required (not silently accepted) --- #
def test_optional_wrong_type_requires_review() -> None:
    r = cf.validate_semantic_contract([_rec(package_quantity={"value": 1})])  # nested where scalar
    assert r.compatibility is cf.SemanticCompatibility.REVIEW_REQUIRED
    assert r.processable is False
    assert r.contract_fingerprint is None


# --- §6.9: broken nesting on an essential -> breaking --------------------------------------- #
def test_broken_nesting_on_essential_is_breaking() -> None:
    r = cf.validate_semantic_contract([_rec(name={"text": "Leche"})])
    assert r.compatibility is cf.SemanticCompatibility.BREAKING
    assert r.processable is False


# --- §6.6: regular absent but a valid promotional price -> mappable ------------------------- #
def test_regular_absent_but_promo_valid_is_mappable() -> None:
    r = cf.validate_semantic_contract([_rec(regular_price=None, promotional_price="0.99")])
    assert r.processable is True
    assert r.rejected_records == 0
    product = ParseBotCarrefourMapper().map_products(
        [_rec(regular_price=None, promotional_price="0.99")], retrieved_at=NOW
    )
    assert len(product) == 1
    assert product[0].regular_price == Decimal("0.99")  # Decimal money


# --- §6.7-8: both prices absent / product_id absent -> the RECORD is rejected --------------- #
def test_missing_price_or_id_rejects_the_record_but_not_the_contract() -> None:
    both_prices = cf.validate_semantic_contract([_rec(regular_price=None, promotional_price=None)])
    assert both_prices.compatibility is cf.SemanticCompatibility.COMPATIBLE
    assert both_prices.rejected_records == 1 and both_prices.mappable_records == 0

    no_id = cf.validate_semantic_contract([_rec(product_id=None)])
    assert no_id.rejected_records == 1

    # The mapper skips the rejected records instead of raising for the whole batch.
    mapped = ParseBotCarrefourMapper().map_products(
        [_rec(), _rec(product_id="CF-9", regular_price=None, promotional_price=None)],
        retrieved_at=NOW,
    )
    assert len(mapped) == 1  # the priced one; the price-less one is dropped


# --- §6.10: an extra unknown field is recorded as raw drift, never silently ignored -------- #
def test_unknown_field_is_recorded_as_raw_drift() -> None:
    r = cf.validate_semantic_contract([_rec(surprise_new_field="x")])
    assert "surprise_new_field" in r.unknown_fields
    assert r.processable is True  # drift alone does not break the contract


# --- The mapper gates on the semantic contract, not the raw fingerprint --------------------- #
def test_mapper_processes_compatible_batch_despite_unknown_raw_fingerprint() -> None:
    batch = [_rec(), _rec(product_id="CF-2", promotional_price="0.89")]
    m = ParseBotCarrefourMapper()
    # This raw fingerprint is NOT in supported_schema_fingerprints, yet the batch maps fine.
    assert m.detect_schema(batch) not in m.supported_schema_fingerprints
    products = m.map_products(batch, retrieved_at=NOW)
    assert len(products) == 2


def test_mapper_blocks_review_required_and_breaking_batches() -> None:
    m = ParseBotCarrefourMapper()
    for bad in ([_rec(brand={"n": "x"})], [_rec(product_id=["nested"])]):
        try:
            m.map_products(bad, retrieved_at=NOW)
            raise AssertionError("expected UnsupportedSchemaError")
        except UnsupportedSchemaError:
            pass


# --- The tolerance is Carrefour-ONLY: other chains keep strict raw pinning ------------------ #
def test_alcampo_still_uses_strict_raw_pinning() -> None:
    # An unrecognized Alcampo batch must still be blocked (no semantic tolerance leaked in).
    try:
        ParseBotAlcampoMapper().map_products([{"productId": "x", "name": "y"}], retrieved_at=NOW)
        raise AssertionError("expected UnsupportedSchemaError")
    except UnsupportedSchemaError:
        pass


# --- Coverage metrics + semantic projection independence ------------------------------------ #
def test_coverage_metrics_and_projection_are_promotion_count_independent() -> None:
    one_promo = [_rec(), _rec(product_id="CF-2", promotional_price="0.89")]
    no_promo = [_rec(), _rec(product_id="CF-2")]
    p1 = cf.semantic_schema_projection(one_promo)
    p2 = cf.semantic_schema_projection(no_promo)
    # promotional_price present in one batch, absent in the other -> projections differ only there,
    # but BOTH are compatible and share the stable contract fingerprint.
    assert cf.validate_semantic_contract(one_promo).contract_fingerprint == STABLE_FP
    assert cf.validate_semantic_contract(no_promo).contract_fingerprint == STABLE_FP
    assert isinstance(p1, dict) and isinstance(p2, dict)
    r = cf.validate_semantic_contract(one_promo)
    assert 0.0 <= r.coverage["price"] <= 1.0
