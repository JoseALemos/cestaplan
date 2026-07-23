"""Parse.bot DIA transport models (spec §4) — derived ONLY from the observed sample.

These mirror exactly what ``/search_products`` returns for DIA (see
docs/PARSEBOT_INTEGRATION.md and the git-ignored capture). Critical fields (present in 10/10
of the sample) are required; fields seen only sometimes are optional. ``extra="ignore"``
tolerates new fields without failing, but a missing critical field raises. Money is Decimal.
Nothing here is assumed beyond the sample: no barcode, package size or observation date are
modelled because DIA's search endpoint does not return them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict


def _to_decimal(value: object) -> object:
    # money arrives as a JSON number; go through str to avoid float imprecision.
    return Decimal(str(value)) if value is not None and not isinstance(value, Decimal) else value


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]


class ParseBotDiaAllergen(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str


class ParseBotDiaPrices(BaseModel):
    """The ``prices`` object (all fields present in every sampled item)."""

    model_config = ConfigDict(extra="ignore")

    currency: str
    price: Money
    strikethrough_price: Money
    price_per_unit: Money
    measure_unit: str
    discount_percentage: int
    is_promo_price: bool
    is_club_price: bool


class ParseBotDiaProduct(BaseModel):
    """One ``search_items`` product. Critical fields required; the rest optional."""

    model_config = ConfigDict(extra="ignore")

    # critical (10/10)
    sku_id: str
    display_name: str
    brand: str
    brand_type: str
    l1_category_description: str
    l2_category_description: str
    image: str
    url: str
    object_id: str
    units_in_stock: int
    units_in_cart: int
    prices: ParseBotDiaPrices
    # optional (seen only sometimes)
    dia_brand: bool | None = None
    allergens: list[ParseBotDiaAllergen] | None = None
    product_info: str | None = None


__all__ = ["Money", "ParseBotDiaAllergen", "ParseBotDiaPrices", "ParseBotDiaProduct"]
