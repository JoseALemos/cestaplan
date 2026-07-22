"""Demo adapter: exposes the seeded synthetic catalogue as normalized records.

All data it yields is synthetic (``source_type='demo'``) and must never be shown as real.
It reads the demo retailer/store/products already seeded by
:mod:`cestaplan_api.scripts.seed_demo`; it does not write or fabricate prices.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.base import (
    AdapterCapabilities,
    AdapterMetadata,
    AdapterStatus,
    NormalizedRecord,
    NotSupportedError,
    RetailerAdapter,
    StoreSelector,
)
from cestaplan_api.models import Product, ProductPrice, Retailer, Store


class DemoRetailerAdapter(RetailerAdapter):
    """Reads the seeded ``MercaEjemplo`` demo catalogue from the database."""

    adapter_key = "demo"
    source_type = "demo"
    enabled = True

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_search=True,
            supports_get_product=True,
            supports_get_price=True,
            supports_store_catalog=True,
            requires_network=False,
            default_source_type="demo",
            retailers=("mercaejemplo",),
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="1.0",
            source_type=self.source_type,
            status=AdapterStatus.ACTIVE,
            enabled=self.enabled,
            data_source_slug="demo-mercaejemplo",
            license_code="synthetic",
            attribution_text="Datos sintéticos de demostración de CestaPlan. No son reales.",
        )

    def get_store_catalog(  # type: ignore[override]
        self, selector: StoreSelector, db: Session | None = None, cursor: str | None = None
    ) -> list[NormalizedRecord]:
        """Yield the seeded demo products with their latest synthetic price.

        Requires a ``db`` session (the demo source lives in the database). Without one it
        cannot read anything and says so explicitly rather than fabricating records.
        """
        if db is None:
            raise NotSupportedError("demo: get_store_catalog requiere una sesión de BD")

        retailer = db.execute(
            select(Retailer).where(Retailer.slug == selector.retailer_slug)
        ).scalar_one_or_none()
        if retailer is None:
            return []

        rows = db.execute(
            select(Product, ProductPrice, Store)
            .join(ProductPrice, ProductPrice.product_id == Product.id)
            .join(Store, Store.id == ProductPrice.store_id)
            .where(Product.retailer_id == retailer.id, Product.is_synthetic.is_(True))
        ).all()

        records: list[NormalizedRecord] = []
        for product, price, store in rows:
            records.append(
                NormalizedRecord(
                    retailer_slug=retailer.slug,
                    store_external_code=store.external_code or "",
                    product_external_id=product.external_id or "",
                    product_name=product.name,
                    package_quantity=price.package_quantity,
                    package_unit=price.package_unit,
                    amount=price.amount,
                    currency=price.currency,
                    source_type="demo",
                    source_name=price.source_name,
                    observed_at=price.observed_at,
                    store_province=store.province,
                    store_locality=store.locality,
                    store_postal_code=store.postal_code,
                    brand=product.brand,
                    category=product.category_code,
                    unit_price=price.unit_price,
                    availability=price.availability,
                    expires_at=price.expires_at,
                    confidence_score=price.confidence_score,
                    verification_status=price.verification_status,
                )
            )
        return records
