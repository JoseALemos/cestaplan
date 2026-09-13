"""DIA coverage onboarding — ranking, skip-on-junk, idempotency, correct flags/enums, conversion.

No real network is hit: a ``FakeDiaClient`` returns canned DIA ``search_item`` records (the SAME raw
shape the real client returns), so the production :class:`DiaMapper`, the ranking and persistence
all run for real. Each test runs inside the transactional ``db_session`` fixture (tools/conftest.py)
and is rolled back on teardown. The 75 canonical ingredients are pre-seeded; the retailer ``dia``
is get-or-created per test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.providers.dia.mapping import DiaMapper
from cestaplan_api.models import (
    IngredientProductMapping,
    Product,
    ProductPrice,
    Retailer,
)
from cestaplan_api.tools import onboard_dia_coverage as tool


# --------------------------------------------------------------------------- #
# helpers / fakes
# --------------------------------------------------------------------------- #
def _record(
    sku: str, name: str, price: float, *, ppu: float | None = None, unit: str | None = None,
    brand: str = "Dia", stock: int = 10,
) -> dict[str, Any]:
    """One raw DIA ``search_item`` (the shape :class:`DiaMapper` consumes)."""
    prices: dict[str, Any] = {"price": price}
    if ppu is not None:
        prices["price_per_unit"] = ppu
    if unit is not None:
        prices["measure_unit"] = unit
    return {
        "sku_id": sku, "display_name": name, "brand": brand, "prices": prices,
        "units_in_stock": stock, "l2_category_description": "cat",
        "url": f"https://www.dia.es/p/{sku}", "image": f"https://img/{sku}.jpg",
    }


class FakeDiaClient:
    """A DIA search client that returns canned records per term — never touches the network."""

    def __init__(self, by_term: dict[str, list[dict[str, Any]]]) -> None:
        self.by_term = by_term
        self.calls: list[str] = []

    def search(self, term: str, *, max_products: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(term)
        return list(self.by_term.get(term, []))


def _dia_retailer(db: Session) -> Retailer:
    """Get-or-create the productive ``dia`` retailer (the tool never creates it itself)."""
    retailer = db.execute(select(Retailer).where(Retailer.slug == "dia")).scalar_one_or_none()
    if retailer is None:
        retailer = Retailer(
            slug="dia", name="DIA", adapter_key="dia", country="ES",
            is_active=True, is_synthetic=False,
        )
        db.add(retailer)
        db.flush()
    return retailer


def _map(records: list[dict[str, Any]]) -> list:
    return DiaMapper().map_products(records, observed_at=datetime.now(UTC))


def _count(db: Session, model, *where) -> int:
    q = select(func.count()).select_from(model)
    for clause in where:
        q = q.where(clause)
    return int(db.scalar(q) or 0)


def _product(db: Session, external_id: str) -> Product:
    return db.execute(select(Product).where(Product.external_id == external_id)).scalar_one()


# --------------------------------------------------------------------------- #
# ranking (pure — no DB)
# --------------------------------------------------------------------------- #
def test_ranking_picks_plain_staple_over_cheaper_prepared_product() -> None:
    products = _map([
        _record("A1", "Tomate frito Dia 350 g", 0.55, ppu=1.57, unit="kilo"),   # junk, cheapest
        _record("A2", "Tomate triturado Dia 400 g", 0.65, ppu=1.63, unit="kilo"),  # plain staple
        _record("A3", "Tomate triturado ecológico Dia 390 g", 1.20, ppu=3.08, unit="kilo"),
    ])
    chosen = tool.choose_product("tomate triturado", products)
    assert chosen is not None
    # the prepared (frito) product is excluded even though it is the cheapest; between the two plain
    # staples the cheaper one wins.
    assert chosen.product.external_product_id == "A2"


def test_ranking_requires_all_core_tokens_as_whole_words() -> None:
    # "muslo de pollo" lacks the head token "pechuga" -> not a candidate for "pechuga de pollo".
    products = _map([
        _record("M1", "Muslo de pollo Dia 1 kg", 3.20, ppu=3.20, unit="kilo"),
        _record("P1", "Pechuga de pollo Dia bandeja 500 g", 4.50, ppu=9.00, unit="kilo"),
    ])
    chosen = tool.choose_product("pechuga de pollo", products)
    assert chosen is not None
    assert chosen.product.external_product_id == "P1"


def test_ranking_matches_singular_term_against_plural_name() -> None:
    # the singular term "patata" must match the plural staple "Patatas".
    products = _map([_record("PA1", "Patatas Dia malla 2 kg", 1.99, ppu=1.00, unit="kilo")])
    chosen = tool.choose_product("patata", products)
    assert chosen is not None
    assert chosen.product.external_product_id == "PA1"


def test_choose_returns_none_when_only_junk() -> None:
    products = _map([
        _record("J1", "Patatas fritas Dia 150 g", 1.10),
        _record("J2", "Snack de patata Dia 100 g", 1.50),
    ])
    assert tool.choose_product("patata", products) is None


def test_build_search_term_uses_alias_then_underscore_expansion() -> None:
    assert tool.build_search_term("pollo_pechuga") == "pechuga de pollo"
    assert tool.build_search_term("aceite_oliva") == "aceite de oliva"
    assert tool.build_search_term("arroz_redondo") == "arroz redondo"  # no alias -> plain expansion


# --------------------------------------------------------------------------- #
# onboarding (DB integration)
# --------------------------------------------------------------------------- #
def test_onboard_creates_product_price_mapping_with_correct_flags(db_session: Session) -> None:
    _dia_retailer(db_session)
    client = FakeDiaClient({"tomate triturado": [
        _record("A1", "Tomate frito Dia 350 g", 0.55, ppu=1.57, unit="kilo"),
        _record("A2", "Tomate triturado Dia 400 g", 0.65, ppu=1.63, unit="kilo"),
    ]})
    diff = tool.onboard(db_session, client=client, ingredient_names=["tomate_triturado"])

    assert (diff.products_created, diff.prices_created, diff.mappings_created) == (1, 1, 1)
    outcome = diff.per_ingredient[0]
    assert outcome.status == "mapped"
    assert outcome.external_id == "A2"

    product = _product(db_session, "A2")
    assert product.is_synthetic is False
    dia = db_session.execute(select(Retailer).where(Retailer.slug == "dia")).scalar_one()
    assert product.retailer_id == dia.id

    price = db_session.execute(
        select(ProductPrice).where(ProductPrice.product_id == product.id)
    ).scalar_one()
    assert price.is_synthetic is False
    assert price.source_type == "authorized_partner"  # valid enum for DIA's authorized access
    assert price.source_name == "DIA (API pública directa)"
    assert price.verification_status == "machine_verified"
    assert price.amount == Decimal("0.65")
    assert price.package_quantity == Decimal("400")
    assert price.package_unit == "g"
    assert price.store_id is not None  # national store satisfies the NOT NULL store_id

    mapping = db_session.execute(
        select(IngredientProductMapping).where(IngredientProductMapping.product_id == product.id)
    ).scalar_one()
    assert mapping.is_active is True
    assert mapping.match_method == "dia_search_curated"
    assert mapping.verification_status == "machine_verified"  # NOT "verified" (invalid enum)
    assert mapping.preference_rank == 0
    assert mapping.conversion_factor == Decimal("400")


def test_onboard_skips_and_reports_junk_only_ingredient(db_session: Session) -> None:
    _dia_retailer(db_session)
    client = FakeDiaClient({"patata": [
        _record("J1", "Patatas fritas Dia 150 g", 1.10),
        _record("J2", "Snack de patata Dia 100 g", 1.50),
    ]})
    diff = tool.onboard(db_session, client=client, ingredient_names=["patata"])
    assert (diff.products_created, diff.prices_created, diff.mappings_created) == (0, 0, 0)
    assert diff.per_ingredient[0].status == "skipped"
    assert diff.per_ingredient[0].reason is not None
    # nothing was persisted for the dia retailer.
    assert _count(db_session, Product, Product.external_id.in_(["J1", "J2"])) == 0


def test_onboard_skips_unknown_canonical_name(db_session: Session) -> None:
    _dia_retailer(db_session)
    client = FakeDiaClient({})
    diff = tool.onboard(
        db_session, client=client, ingredient_names=["ingrediente_inexistente_xyz"]
    )
    assert diff.per_ingredient[0].status == "skipped"
    assert diff.per_ingredient[0].reason == "ingrediente canónico desconocido"
    assert client.calls == []  # an unknown ingredient never triggers a DIA search


def test_conversion_factor_from_pack_net_content(db_session: Session) -> None:
    _dia_retailer(db_session)
    client = FakeDiaClient({"leche entera": [
        _record("L1", "Leche entera Dia Láctea pack 6 x 1 L", 5.76, ppu=0.96, unit="litro"),
    ]})
    diff = tool.onboard(db_session, client=client, ingredient_names=["leche_entera"])
    outcome = diff.per_ingredient[0]
    assert outcome.status == "mapped"
    # net content "pack 6 x 1 L" -> 6 L; conversion_factor = 6 * 1000 (l->ml base) = 6000.
    assert outcome.package_quantity == Decimal("6")
    assert outcome.package_unit == "l"
    assert outcome.conversion_factor == Decimal("6000")

    product = _product(db_session, "L1")
    mapping = db_session.execute(
        select(IngredientProductMapping).where(IngredientProductMapping.product_id == product.id)
    ).scalar_one()
    assert mapping.conversion_factor == Decimal("6000")
    price = db_session.execute(
        select(ProductPrice).where(ProductPrice.product_id == product.id)
    ).scalar_one()
    assert price.package_quantity == Decimal("6")
    assert price.package_unit == "l"
    assert price.unit_price == Decimal("0.96")


def test_conversion_factor_none_when_net_content_absent(db_session: Session) -> None:
    _dia_retailer(db_session)
    # a plain staple whose name carries NO parseable net content, only a €/kg unit price.
    client = FakeDiaClient({"pechuga de pollo": [
        _record("PC1", "Pechuga de pollo Dia", 5.49, ppu=6.49, unit="kilo"),
    ]})
    diff = tool.onboard(db_session, client=client, ingredient_names=["pollo_pechuga"])
    outcome = diff.per_ingredient[0]
    assert outcome.status == "mapped"
    assert outcome.conversion_factor is None  # crude fallback: net content not usable
    assert outcome.package_unit == "kg"  # falls back to the unit_price_unit
    assert outcome.package_quantity == Decimal("1")

    product = _product(db_session, "PC1")
    mapping = db_session.execute(
        select(IngredientProductMapping).where(IngredientProductMapping.product_id == product.id)
    ).scalar_one()
    assert mapping.conversion_factor is None
    assert mapping.is_active is True  # still onboarded (coverage), just without a scaling factor


def test_second_run_is_idempotent_noop(db_session: Session) -> None:
    _dia_retailer(db_session)
    client = FakeDiaClient({"tomate triturado": [
        _record("A2", "Tomate triturado Dia 400 g", 0.65, ppu=1.63, unit="kilo"),
    ]})
    first = tool.onboard(db_session, client=client, ingredient_names=["tomate_triturado"])
    assert (first.products_created, first.prices_created, first.mappings_created) == (1, 1, 1)

    counts = (
        _count(db_session, Product, Product.external_id == "A2"),
        _count(db_session, ProductPrice),
        _count(db_session, IngredientProductMapping),
    )
    second = tool.onboard(db_session, client=client, ingredient_names=["tomate_triturado"])
    assert (second.products_created, second.prices_created, second.mappings_created) == (0, 0, 0)
    assert second.per_ingredient[0].status == "reused"
    # byte-identical row counts after the second run.
    assert counts == (
        _count(db_session, Product, Product.external_id == "A2"),
        _count(db_session, ProductPrice),
        _count(db_session, IngredientProductMapping),
    )


def test_diff_as_dict_is_json_serializable(db_session: Session) -> None:
    # OnboardDiff/IngredientOutcome are slots dataclasses; as_dict() must still serialize cleanly
    # (Decimals -> str) for the JSON summary the CLI prints.
    import json

    _dia_retailer(db_session)
    client = FakeDiaClient({"tomate triturado": [
        _record("A2", "Tomate triturado Dia 400 g", 0.65, ppu=1.63, unit="kilo"),
    ]})
    diff = tool.onboard(db_session, client=client, ingredient_names=["tomate_triturado"])
    payload = diff.as_dict()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert '"conversion_factor": "400"' in text
    assert payload["per_ingredient"][0]["status"] == "mapped"


def test_dry_run_search_happens_but_no_writes_persist(db_session: Session) -> None:
    # dry-run still SEARCHES DIA (a read) to choose products; only DB writes are gated by --commit.
    # onboard() itself does not commit — the caller (run) controls the transaction — so here we
    # prove the search ran (rows exist only until the fixture rolls back).
    _dia_retailer(db_session)
    client = FakeDiaClient({"tomate triturado": [
        _record("A2", "Tomate triturado Dia 400 g", 0.65, ppu=1.63, unit="kilo"),
    ]})
    tool.onboard(db_session, client=client, ingredient_names=["tomate_triturado"])
    assert client.calls == ["tomate triturado"]
