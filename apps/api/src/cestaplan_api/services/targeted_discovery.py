"""Targeted product discovery + auditable mapping for priority ingredients (spec §5/§6/§7).

For ``parsebot-alcampo`` (has keyword search) each ingredient is searched once (<=10 results,
one page); for ``parsebot-carrefour`` (no keyword search) the already-staged products are
re-used and classified locally — no extra external calls. Every candidate product is persisted
as a STAGING variant (never production) plus an auditable :class:`ProviderIngredientMapping`.
Discovery is review-only by default: nothing becomes ``active``; every match is a ``candidate``
awaiting human review, with the machine proposal kept in ``proposed_*``. Deterministic
auto-approval is an explicit opt-in and never the cloud default (see :class:`ApprovalMode`).

Real captures are written under ``.local/provider-targeted-coverage/`` (git-ignored); synthetic
fixtures are never replaced.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct
from cestaplan_api.ingestion.providers.onboarding import classify_costing_mode, get_entry
from cestaplan_api.ingestion.providers.registry import registry
from cestaplan_api.models import (
    ExternalProduct,
    Ingredient,
    PriceObservation,
    Product,
    ProductVariant,
    ProviderIngredientMapping,
    ProviderUsage,
    Retailer,
)
from cestaplan_api.services.ingredient_dictionary import (
    classify_mapping,
    normalize_provider_category,
    specs,
)

_LOCAL = Path("/root/cestaplan/.local/provider-targeted-coverage")


@dataclass(slots=True)
class IngredientDiscovery:
    ingredient_key: str
    candidates: int = 0
    auto_approved: int = 0
    review: int = 0
    rejected: int = 0
    costable: int = 0


@dataclass(slots=True)
class DiscoveryReport:
    provider_code: str
    retailer_slug: str
    queries: int = 0
    products_seen: int = 0
    api_calls: int = 0
    per_ingredient: list[IngredientDiscovery] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_code": self.provider_code,
            "retailer_slug": self.retailer_slug,
            "queries": self.queries,
            "products_seen": self.products_seen,
            "api_calls": self.api_calls,
            "per_ingredient": [asdict(d) for d in self.per_ingredient],
        }


def _ingredient_ids(db: Session, keys: list[str]) -> dict[str, int]:
    rows = db.execute(
        select(Ingredient.canonical_name, Ingredient.id).where(Ingredient.canonical_name.in_(keys))
    ).all()
    return {row[0]: row[1] for row in rows}


def _persist_product(
    db: Session, retailer_id: int, p: ExternalCatalogProduct, *, now: datetime
) -> tuple[int, int]:
    """Upsert ExternalProduct+Product+ProductVariant + a STAGING price. Returns (product_id, vid).

    Persists ONLY staging observations (never production).
    """
    external = db.execute(
        select(ExternalProduct).where(
            ExternalProduct.retailer_id == retailer_id,
            ExternalProduct.external_id == p.external_product_id,
        )
    ).scalar_one_or_none()
    if external is None:
        external = ExternalProduct(retailer_id=retailer_id, external_id=p.external_product_id)
        db.add(external)
        db.flush()
    product = (
        db.execute(
            select(Product).where(Product.id == external.canonical_product_id)
        ).scalar_one_or_none()
        if external.canonical_product_id
        else None
    )
    if product is None:
        product = Product(name=p.product_name[:200] or "producto", is_synthetic=False)
        db.add(product)
        db.flush()
        external.canonical_product_id = product.id
    variant = db.execute(
        select(ProductVariant).where(ProductVariant.external_product_id == external.id)
    ).scalar_one_or_none()
    if variant is None:
        variant = ProductVariant(
            retailer_id=retailer_id,
            external_product_id=external.id,
            product_id=product.id,
            display_name=p.product_name[:200] or "variante",
        )
        db.add(variant)
    variant.product_id = product.id
    variant.sell_unit = p.sell_unit.value
    variant.variable_weight = p.variable_weight
    variant.net_content_quantity = p.net_content_quantity
    variant.net_content_unit = p.net_content_unit.value if p.net_content_unit else None
    variant.unit_price = p.unit_price
    variant.unit_price_unit = p.unit_price_unit
    db.flush()
    # A staging observation (never production).
    db.add(
        PriceObservation(
            retailer_id=retailer_id,
            product_variant_id=variant.id,
            price_scope=p.price_scope.value,
            price_type="regular",
            amount=p.regular_price,
            currency=p.currency,
            observed_at=p.observed_at,
            imported_at=now,
            valid_from=now,
            confidence_score=Decimal("1.0"),
            staging_only=True,
        )
    )
    db.flush()
    return product.id, variant.id


def _classify_best(product: ExternalCatalogProduct, keys: list[str]) -> tuple[str, object] | None:
    """Best ingredient match for a product across the given keys (None if all incompatible)."""
    best: tuple[str, object] | None = None
    best_conf = Decimal("-1")
    for key in keys:
        cand = classify_mapping(
            key,
            product_name=product.product_name,
            brand=product.brand,
            category_code=normalize_provider_category(product.category),
            net_content_unit=product.net_content_unit.value if product.net_content_unit else None,
        )
        if cand.mapping_status in ("incompatible", "rejected"):
            continue
        if cand.confidence > best_conf:
            best, best_conf = (key, cand), cand.confidence
    return best


def _capture_alcampo(
    provider_code: str, settings: Settings, key: str, limit: int, out_dir: Path
) -> list[ExternalCatalogProduct]:
    from cestaplan_api.ingestion.providers.parsebot import plans

    alias = specs()[key].aliases[0]
    records = plans.capture_records(provider_code, settings, limit=limit, query=alias)
    # Persist the raw capture per ingredient for audit (git-ignored; never versioned).
    ing_dir = out_dir / key
    ing_dir.mkdir(parents=True, exist_ok=True)
    (ing_dir / "capture.json").write_text(
        json.dumps(
            {"query": alias, "count": len(records), "records": records},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    provider = registry.get(provider_code)
    return list(provider._mapper.map_products(records, retrieved_at=datetime.now(UTC)))  # type: ignore[attr-defined]


class ApprovalMode(StrEnum):
    """How discovery persists matches.

    ``REVIEW_ONLY`` (default) never activates a mapping: every compatible match is stored as a
    ``candidate`` needing human review, with the machine's original proposal kept in the
    ``proposed_*`` fields. ``DETERMINISTIC_AUTOAPPROVAL`` restores the legacy behaviour (exact
    deterministic matches become ``active``) and must be requested explicitly — it is never the
    cloud default.
    """

    REVIEW_ONLY = "review_only"
    DETERMINISTIC_AUTOAPPROVAL = "deterministic_autoapproval"


# Conservative, tunable bounds so a generic term never relates one product to dozens of ingredients
# (candidate explosion). Excess is dropped and surfaced in metrics, never silently hidden.
_MAX_CANDIDATES_PER_PRODUCT = 3
_MAX_CANDIDATES_PER_INGREDIENT = 25
_MIN_REVIEWABLE_CONFIDENCE = Decimal("0.30")
# Mapping-algorithm version: a re-run under a new version supersedes the prior candidates (§6).
_MAPPING_VERSION = "2.0.0"


def _classify_candidates(
    product: ExternalCatalogProduct, keys: list[str]
) -> list[tuple[str, object]]:
    """All COMPATIBLE ``(key, candidate)`` for a product, ranked by confidence desc. Hard
    incompatibles (family/unit/prep/diet/allergen/rejected) are dropped; a product is classified
    exactly ONCE against the whole ingredient set."""
    known = specs()
    out: list[tuple[str, object]] = []
    for key in keys:
        if key not in known:
            continue  # not in the ingredient dictionary -> cannot classify (never fabricate)
        cand = classify_mapping(
            key,
            product_name=product.product_name,
            brand=product.brand,
            category_code=normalize_provider_category(product.category),
            net_content_unit=product.net_content_unit.value if product.net_content_unit else None,
        )
        if cand.mapping_status in ("incompatible", "rejected"):
            continue
        out.append((key, cand))
    out.sort(key=lambda kc: kc[1].confidence, reverse=True)  # type: ignore[attr-defined]
    return out


def discover_and_map(
    db: Session,
    provider_code: str,
    ingredient_keys: list[str],
    *,
    settings: Settings | None = None,
    per_query_limit: int = 10,
    max_calls: int = 10,
    now: datetime | None = None,
    approval_mode: ApprovalMode = ApprovalMode.REVIEW_ONLY,
) -> DiscoveryReport:
    """Discover + map priority ingredients for one provider (staging only, never production)."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    entry = get_entry(provider_code)
    retailer_slug = entry.retailer_slug if entry else provider_code
    retailer_id = db.execute(
        select(Retailer.id).where(Retailer.slug == retailer_slug)
    ).scalar_one_or_none()
    report = DiscoveryReport(provider_code, retailer_slug)
    if retailer_id is None:
        return report
    ing_ids = _ingredient_ids(db, ingredient_keys)
    out_dir = _LOCAL / provider_code

    # 1) INGESTION happens ONCE per unique product and NEVER during matching. Search providers
    #    (Alcampo/DIA) capture then persist once per unique product; staged providers (Carrefour)
    #    reuse the EXISTING product ids and write nothing — matching never creates an observation.
    products: list[tuple[ExternalCatalogProduct, int]] = []
    if provider_code in ("parsebot-alcampo", "parsebot-dia"):
        out_dir.mkdir(parents=True, exist_ok=True)  # capture files: only the search-based providers
        captured: dict[str, ExternalCatalogProduct] = {}
        for key in ingredient_keys:
            if report.api_calls >= max_calls:
                break
            try:
                fetched = _capture_alcampo(provider_code, settings, key, per_query_limit, out_dir)
                report.api_calls += 1
                report.queries += 1
                _log_usage(db, provider_code, len(fetched), now)
            except Exception:
                fetched = []
            for p in fetched:
                captured.setdefault(p.external_product_id, p)  # one observation per unique product
            (out_dir / f"{key}.count").write_text(str(len(fetched)))
        for p in captured.values():
            product_id, _vid = _persist_product(db, retailer_id, p, now=now)
            products.append((p, product_id))
    else:
        products = [(dto, pid) for dto, pid, _vid in _staged_products(db, retailer_id)]

    report.products_seen = len(products)

    # 2) MATCHING reads + writes ONLY candidate rows. Each product is classified ONCE against all
    #    ingredients; candidates are bounded per product and per ingredient so a generic term cannot
    #    relate one product to dozens of ingredients (excess is dropped, surfaced in metrics).
    per_key = {key: IngredientDiscovery(key) for key in ingredient_keys}
    report.per_ingredient = list(per_key.values())
    per_ingredient_count: Counter[str] = Counter()
    for dto, product_id in products:
        costable = classify_costing_mode(dto).value != "unresolved"
        kept = [
            (key, cand)
            for key, cand in _classify_candidates(dto, ingredient_keys)
            if cand.confidence >= _MIN_REVIEWABLE_CONFIDENCE  # type: ignore[attr-defined]
        ][:_MAX_CANDIDATES_PER_PRODUCT]
        for key, cand in kept:
            ing_id = ing_ids.get(key)
            if ing_id is None or per_ingredient_count[key] >= _MAX_CANDIDATES_PER_INGREDIENT:
                continue
            status = cand.mapping_status  # type: ignore[attr-defined]
            active = (
                approval_mode is ApprovalMode.DETERMINISTIC_AUTOAPPROVAL
                and status == "auto_approved"
            )
            _upsert_mapping(
                db,
                provider_code,
                retailer_slug,
                ing_id,
                key,
                dto,
                product_id,
                cand,
                active=active,
                now=now,
                approval_mode=approval_mode,
                mapping_version=_MAPPING_VERSION,
            )
            per_ingredient_count[key] += 1
            d = per_key[key]
            d.candidates += 1
            d.costable += costable
            if status == "auto_approved":
                d.auto_approved += 1
            elif status in ("candidate",):
                d.review += 1
            else:
                d.rejected += 1
    return report


def _staged_products(
    db: Session, retailer_id: int
) -> list[tuple[ExternalCatalogProduct, int, int]]:
    """Existing staged products as ``(dto, product_id, variant_id)``, ONE per variant.

    The DTO carries the REAL provider ``external_id`` (via ``ExternalProduct``) — never the FK id —
    and the caller gets the EXISTING ``product_id``/``variant_id`` so the mapping layer uses them
    WITHOUT re-persisting anything. Reading staged data never writes a new observation/product.
    """
    from cestaplan_api.ingestion.providers.contracts import (
        Availability,
        ContentUnit,
        PriceScope,
        SellUnit,
    )

    rows = db.execute(
        select(ProductVariant, ExternalProduct.external_id, PriceObservation)
        .join(ExternalProduct, ExternalProduct.id == ProductVariant.external_product_id)
        .join(PriceObservation, PriceObservation.product_variant_id == ProductVariant.id)
        .where(ProductVariant.retailer_id == retailer_id, PriceObservation.staging_only.is_(True))
        .order_by(
            ProductVariant.id,
            PriceObservation.observed_at.desc(),
            PriceObservation.id.desc(),
        )
    ).all()
    out: list[tuple[ExternalCatalogProduct, int, int]] = []
    seen_variants: set[int] = set()
    for v, external_id, obs in rows:
        if v.id in seen_variants or v.product_id is None:
            continue
        seen_variants.add(v.id)  # one DTO per variant (its latest staging observation)
        dto = ExternalCatalogProduct(
            provider="staged",
            retailer_slug="",
            external_product_id=str(external_id),
            product_name=v.display_name,
            sell_unit=SellUnit(v.sell_unit or "package"),
            regular_price=obs.amount,
            currency=obs.currency,
            price_scope=PriceScope(obs.price_scope),
            observed_at=obs.observed_at,
            availability=Availability.UNKNOWN,
            variable_weight=v.variable_weight,
            net_content_quantity=v.net_content_quantity,
            net_content_unit=ContentUnit(v.net_content_unit) if v.net_content_unit else None,
            unit_price=v.unit_price,
            unit_price_unit=v.unit_price_unit,
        )
        out.append((dto, v.product_id, v.id))
    return out


def _upsert_mapping(
    db: Session,
    provider_code: str,
    retailer_slug: str,
    ingredient_id: int,
    key: str,
    product: ExternalCatalogProduct,
    product_id: int,
    cand: object,
    *,
    active: bool,
    now: datetime,
    approval_mode: ApprovalMode,
    mapping_version: str = "1.0.0",
) -> None:
    row = db.execute(
        select(ProviderIngredientMapping).where(
            ProviderIngredientMapping.provider_code == provider_code,
            ProviderIngredientMapping.ingredient_id == ingredient_id,
            ProviderIngredientMapping.external_product_id == product.external_product_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = ProviderIngredientMapping(
            provider_code=provider_code,
            ingredient_id=ingredient_id,
            external_product_id=product.external_product_id,
        )
        db.add(row)
    row.canonical_ingredient_key = key
    row.retailer_slug = retailer_slug
    row.normalized_product_id = product_id
    row.mapping_version = mapping_version
    row.mapping_method = cand.mapping_method  # type: ignore[attr-defined]
    row.confidence_score = cand.confidence  # type: ignore[attr-defined]
    row.lexical_score = cand.lexical_score  # type: ignore[attr-defined]
    row.category_score = cand.category_score  # type: ignore[attr-defined]
    row.semantic_score = cand.semantic_score  # type: ignore[attr-defined]
    row.unit_compatibility = cand.unit_compatibility  # type: ignore[attr-defined]
    row.preparation_compatibility = cand.preparation_compatibility  # type: ignore[attr-defined]
    row.dietary_compatibility = cand.dietary_compatibility  # type: ignore[attr-defined]
    row.allergen_compatibility = cand.allergen_compatibility  # type: ignore[attr-defined]
    # The machine's original proposal is ALWAYS recorded, for audit and for the reviewer.
    row.proposed_mapping_status = cand.mapping_status  # type: ignore[attr-defined]
    row.proposed_confidence = cand.confidence  # type: ignore[attr-defined]
    row.proposed_method = cand.mapping_method  # type: ignore[attr-defined]
    if approval_mode is ApprovalMode.REVIEW_ONLY:
        # Never approved, never active: a candidate awaiting human review, whatever the proposal.
        row.mapping_status = "candidate"
        row.required_review = True
        row.active = False
        row.reviewed_at = None
        row.reviewed_by = None
    else:
        row.mapping_status = cand.mapping_status  # type: ignore[attr-defined]
        row.required_review = cand.required_review  # type: ignore[attr-defined]
        row.active = active
        if active:
            row.reviewed_at = now  # deterministic auto-approval is self-documenting
    row.evidence_json = {"product_name": product.product_name, **cand.as_dict()}  # type: ignore[attr-defined]
    db.flush()


def _log_usage(db: Session, provider_code: str, product_count: int, now: datetime) -> None:
    db.add(
        ProviderUsage(
            provider=provider_code,
            operation="targeted_discovery",
            request_count=1,
            product_count=product_count,
            started_at=now,
            completed_at=now,
        )
    )
    db.flush()


def write_report(report: DiscoveryReport) -> None:
    out = _LOCAL / report.provider_code / "discovery-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))


__all__ = [
    "ApprovalMode",
    "DiscoveryReport",
    "IngredientDiscovery",
    "discover_and_map",
    "write_report",
]
