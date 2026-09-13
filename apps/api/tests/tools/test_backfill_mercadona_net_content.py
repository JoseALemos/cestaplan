"""Mercadona net-content backfill — updates placeholder rows, never guesses, idempotent, scoped.

No real network is hit: the net-content source is injected. Most tests inject a plain
``{external_id: (quantity, unit)}`` index and drive the inner ``backfill(session, ...)`` against the
transactional ``db_session`` fixture (tools/conftest.py), which rolls back on teardown. One test
drives ``build_net_content_index`` with a fake provider that returns ``ExternalCatalogProduct``
objects (some with net content, some without) to prove the crawl->index wiring offline.

The engine reads ``ProductPrice.package_quantity`` + ``package_unit`` to scale a recipe line's cost
(see the tool's module docstring), so the assertions target those fields on the specific rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import (
    ContentUnit,
    ExternalCatalogProduct,
    ProductQuery,
    SellUnit,
)
from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.tools import backfill_mercadona_net_content as tool


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #
def _retailer(db: Session, slug: str) -> Retailer:
    retailer = db.execute(select(Retailer).where(Retailer.slug == slug)).scalar_one_or_none()
    if retailer is None:
        retailer = Retailer(
            slug=slug, name=slug.title(), adapter_key=slug, country="ES",
            is_active=True, is_synthetic=False,
        )
        db.add(retailer)
        db.flush()
    return retailer


def _store(db: Session, retailer: Retailer) -> Store:
    store = Store(retailer_id=retailer.id, name=f"{retailer.slug} zona", is_active=True)
    db.add(store)
    db.flush()
    return store


def _ingredient(db: Session, canonical: str) -> Ingredient:
    ing = db.execute(
        select(Ingredient).where(Ingredient.canonical_name == canonical)
    ).scalar_one_or_none()
    if ing is None:
        ing = Ingredient(canonical_name=canonical, display_name=canonical.replace("_", " ").title())
        db.add(ing)
        db.flush()
    return ing


def _product(db: Session, retailer: Retailer, external_id: str | None, name: str) -> Product:
    product = Product(
        retailer_id=retailer.id, external_id=external_id, name=name, is_synthetic=False,
    )
    db.add(product)
    db.flush()
    return product


def _mapping(db: Session, ingredient: Ingredient, product: Product, retailer: Retailer) -> None:
    db.add(IngredientProductMapping(
        ingredient_id=ingredient.id, product_id=product.id, retailer_id=retailer.id,
        preference_rank=0, match_method="mercadona_direct", verification_status="machine_verified",
        is_active=True,
    ))
    db.flush()


def _price(
    db: Session, retailer: Retailer, store: Store, product: Product, *,
    package_quantity: Decimal = Decimal("1"), package_unit: str = "unit",
    is_synthetic: bool = False,
) -> ProductPrice:
    now = datetime.now(UTC)
    price = ProductPrice(
        retailer_id=retailer.id, store_id=store.id, product_id=product.id,
        amount=Decimal("1.50"), currency="EUR",
        package_quantity=package_quantity, package_unit=package_unit,
        source_type="community_connector", source_name="ingestion",
        observed_at=now, imported_at=now, confidence_score=Decimal("1.0"),
        is_synthetic=is_synthetic,
    )
    db.add(price)
    db.flush()
    return price


def _seed_mapped_product(
    db: Session, retailer: Retailer, store: Store, *, canonical: str, external_id: str | None,
    name: str,
) -> tuple[Product, ProductPrice]:
    """A Mercadona product + active mapping + one placeholder productive price (1/'unit')."""
    ingredient = _ingredient(db, canonical)
    product = _product(db, retailer, external_id, name)
    _mapping(db, ingredient, product, retailer)
    price = _price(db, retailer, store, product)
    return product, price


def _outcome(diff: tool.BackfillDiff, external_id: str | None) -> tool.ProductOutcome:
    return next(o for o in diff.per_product if o.external_id == external_id)


def _ext_product(
    external_id: str, name: str, net_qty: Decimal | None, net_unit: ContentUnit | None,
) -> ExternalCatalogProduct:
    return ExternalCatalogProduct(
        provider="apify-mercadona", retailer_slug="mercadona", external_product_id=external_id,
        product_name=name, sell_unit=SellUnit.PACKAGE, regular_price=Decimal("1.50"),
        currency="EUR", price_scope=PriceScope.POSTAL_CODE, observed_at=datetime.now(UTC),
        net_content_quantity=net_qty, net_content_unit=net_unit,
    )


class FakeProvider:
    """A catalog source that returns canned mapped products — never touches the network."""

    def __init__(self, products: list[ExternalCatalogProduct]) -> None:
        self._products = products
        self.queries: list[ProductQuery] = []

    def iterate_products(self, query: ProductQuery) -> list[ExternalCatalogProduct]:
        self.queries.append(query)
        return list(self._products)


# --------------------------------------------------------------------------- #
# backfill (DB integration)
# --------------------------------------------------------------------------- #
def test_updates_placeholder_prices_to_real_net_content(db_session: Session) -> None:
    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    _, milk_price = _seed_mapped_product(
        db_session, retailer, store,
        canonical="leche_entera", external_id="MERCA-milk", name="Leche entera Hacendado 1 L",
    )
    _, oil_price = _seed_mapped_product(
        db_session, retailer, store,
        canonical="aceite_oliva", external_id="MERCA-oil",
        name="Aceite de oliva virgen extra Hacendado 1 L",
    )

    diff = tool.backfill(db_session, net_content={
        "MERCA-milk": (Decimal("1"), "l"),
        "MERCA-oil": (Decimal("1"), "l"),
    })

    assert diff.products_updated == 2
    assert diff.rows_updated == 2
    assert _outcome(diff, "MERCA-milk").status == "updated"
    assert _outcome(diff, "MERCA-milk").old_package_unit == "unit"
    assert _outcome(diff, "MERCA-milk").new_package_unit == "l"

    db_session.refresh(milk_price)
    db_session.refresh(oil_price)
    assert (milk_price.package_quantity, milk_price.package_unit) == (Decimal("1"), "l")
    assert (oil_price.package_quantity, oil_price.package_unit) == (Decimal("1"), "l")


def test_writes_non_unit_net_content_verbatim(db_session: Session) -> None:
    # a mass product: 500 g flour -> package_quantity=500, package_unit='g' (raw net content, the
    # convention the DIA rows use and the engine expects; it converts the recipe qty into 'g').
    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    _, price = _seed_mapped_product(
        db_session, retailer, store,
        canonical="harina_trigo", external_id="MERCA-flour", name="Harina de trigo Hacendado 500 g",
    )
    tool.backfill(db_session, net_content={"MERCA-flour": (Decimal("500"), "g")})
    db_session.refresh(price)
    assert (price.package_quantity, price.package_unit) == (Decimal("500"), "g")


def test_skips_product_absent_from_catalog(db_session: Session) -> None:
    # a pack the mapper dropped (inconsistent unit_size/reference_price) never lands in the index.
    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    _, price = _seed_mapped_product(
        db_session, retailer, store,
        canonical="leche_entera", external_id="MERCA-pack",
        name="Leche entera Hacendado pack 6 x 1 L",
    )
    diff = tool.backfill(db_session, net_content={})  # empty catalog -> nothing resolvable

    assert diff.products_updated == 0
    assert _outcome(diff, "MERCA-pack").status == "skipped"
    db_session.refresh(price)
    assert (price.package_quantity, price.package_unit) == (Decimal("1"), "unit")  # untouched


def test_skips_product_without_external_id(db_session: Session) -> None:
    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    _, price = _seed_mapped_product(
        db_session, retailer, store,
        canonical="tomate", external_id=None, name="Tomate frito sin id",
    )
    diff = tool.backfill(db_session, net_content={"whatever": (Decimal("1"), "l")})
    assert _outcome(diff, None).status == "skipped"
    assert "external_id" in (_outcome(diff, None).reason or "")
    db_session.refresh(price)
    assert (price.package_quantity, price.package_unit) == (Decimal("1"), "unit")


def test_never_guesses_unscalable_unit(db_session: Session) -> None:
    # a counted net content ('unit') is NOT scalable by the engine -> skipped, never written (which
    # would also re-trip the stale filter and break idempotency).
    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    _, price = _seed_mapped_product(
        db_session, retailer, store,
        canonical="huevo", external_id="MERCA-eggs", name="Huevos Hacendado 12 ud",
    )
    diff = tool.backfill(db_session, net_content={"MERCA-eggs": (Decimal("12"), "unit")})
    assert _outcome(diff, "MERCA-eggs").status == "skipped"
    db_session.refresh(price)
    assert (price.package_quantity, price.package_unit) == (Decimal("1"), "unit")


def test_second_run_is_idempotent_noop(db_session: Session) -> None:
    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    _, price = _seed_mapped_product(
        db_session, retailer, store,
        canonical="leche_entera", external_id="MERCA-milk", name="Leche entera Hacendado 1 L",
    )
    index = {"MERCA-milk": (Decimal("1"), "l")}

    first = tool.backfill(db_session, net_content=index)
    assert first.rows_updated == 1

    second = tool.backfill(db_session, net_content=index)
    assert second.rows_updated == 0
    assert second.products_updated == 0
    assert _outcome(second, "MERCA-milk").status == "skipped"  # no longer a placeholder row
    db_session.refresh(price)
    assert (price.package_quantity, price.package_unit) == (Decimal("1"), "l")


def test_dia_and_synthetic_rows_are_never_touched(db_session: Session) -> None:
    mercadona = _retailer(db_session, "mercadona")
    dia = _retailer(db_session, "dia")
    merca_store = _store(db_session, mercadona)
    dia_store = _store(db_session, dia)

    # A real Mercadona product with BOTH a synthetic and a productive placeholder price.
    ingredient = _ingredient(db_session, "leche_entera")
    merca_product = _product(db_session, mercadona, "MERCA-milk", "Leche entera Hacendado 1 L")
    _mapping(db_session, ingredient, merca_product, mercadona)
    productive = _price(db_session, mercadona, merca_store, merca_product)
    synthetic = _price(db_session, mercadona, merca_store, merca_product, is_synthetic=True)

    # A DIA product+mapping+price that shares the external id, also a placeholder.
    dia_ingredient = _ingredient(db_session, "aceite_oliva")
    dia_product = _product(db_session, dia, "MERCA-milk", "Leche entera Dia 1 L")
    _mapping(db_session, dia_ingredient, dia_product, dia)
    dia_price = _price(db_session, dia, dia_store, dia_product)

    tool.backfill(db_session, net_content={"MERCA-milk": (Decimal("1"), "l")})

    db_session.refresh(productive)
    db_session.refresh(synthetic)
    db_session.refresh(dia_price)
    # only the productive Mercadona row is updated.
    assert (productive.package_quantity, productive.package_unit) == (Decimal("1"), "l")
    assert (synthetic.package_quantity, synthetic.package_unit) == (Decimal("1"), "unit")
    assert (dia_price.package_quantity, dia_price.package_unit) == (Decimal("1"), "unit")


def test_conversion_factor_and_product_are_left_untouched(db_session: Session) -> None:
    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    ingredient = _ingredient(db_session, "leche_entera")
    product = _product(db_session, retailer, "MERCA-milk", "Leche entera Hacendado 1 L")
    _mapping(db_session, ingredient, product, retailer)
    _price(db_session, retailer, store, product)

    tool.backfill(db_session, net_content={"MERCA-milk": (Decimal("1"), "l")})

    mapping = db_session.execute(
        select(IngredientProductMapping).where(IngredientProductMapping.product_id == product.id)
    ).scalar_one()
    db_session.refresh(product)
    assert mapping.conversion_factor is None  # the live rail never reads it; the tool never sets it
    assert product.package_quantity is None  # only ProductPrice is backfilled, not Product
    assert product.package_unit is None


# --------------------------------------------------------------------------- #
# net-content index (offline crawl wiring)
# --------------------------------------------------------------------------- #
def test_build_net_content_index_keys_by_external_id_and_drops_missing() -> None:
    provider = FakeProvider([
        _ext_product("A1", "Leche entera 1 L", Decimal("1"), ContentUnit.L),
        _ext_product("A2", "Harina 1 kg", Decimal("1"), ContentUnit.KG),
        _ext_product("A3", "Pack sin contenido", None, None),  # mapper dropped -> excluded
    ])
    index = tool.build_net_content_index(provider=provider)

    assert index == {"A1": (Decimal("1"), "l"), "A2": (Decimal("1"), "kg")}
    assert "A3" not in index
    assert provider.queries and provider.queries[0].max_products is None


def test_diff_as_dict_is_json_serializable(db_session: Session) -> None:
    import json

    retailer = _retailer(db_session, "mercadona")
    store = _store(db_session, retailer)
    _seed_mapped_product(
        db_session, retailer, store,
        canonical="leche_entera", external_id="MERCA-milk", name="Leche entera Hacendado 1 L",
    )
    diff = tool.backfill(db_session, net_content={"MERCA-milk": (Decimal("1"), "l")})
    payload = diff.as_dict()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert '"new_package_quantity": "1"' in text
    assert payload["per_product"][0]["new_package_unit"] == "l"
