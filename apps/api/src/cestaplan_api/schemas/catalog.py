"""Typed response schemas for the catalog router's public price-provider listing.

Rights/authorization are kept on strictly separate axes from technical availability, quality,
coverage, costing and production activation — the UI must never collapse them into one label.
No internal/secret field (evidence reference, internal notes, verifier user id) is ever a field
here. Decimals/ratios are serialised as strings.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RightsScopeOut(BaseModel):
    """Explicit, orthogonal permissions the recorded rights grant.

    ``attribution_required = null`` means "governed by a private agreement", NOT "attribution is
    unnecessary". ``raw_redistribution`` stays false unless a differentiated authorization exists.
    """

    api_access: bool | None = None
    storage: bool | None = None
    processing: bool | None = None
    display: bool | None = None
    commercial_use: bool | None = None
    derived_results: bool | None = None
    raw_redistribution: bool | None = None
    attribution_required: bool | None = None


class PriceProviderOut(BaseModel):
    """One price source for the chain selector / sources screen.

    Distinct axes, never merged: (1) legal rights — ``authorization_status`` / rights display /
    ``rights_scope``; (2) technical transport/mapper/quality; (3) observed coverage; (4) costing
    eligibility; (5) production activation (``production_enabled`` AND ``production_approved``).
    An authorized-but-incomplete source shows as authorized + experimental, never legally blocked.
    """

    # Identity / ownership
    provider: str
    provider_display_name: str
    retailer: str
    retailer_display_name: str
    retailer_id: str | None = None
    # Technical provider is the intermediary (Parse.bot / Apify); the chain is the data owner.
    technical_provider: str | None = None
    source_type: str
    source_url: str | None = None
    # official_api is true ONLY for a genuine official API of the owner; an intermediary is not.
    official_api: bool
    authorized_source: bool

    # Legal rights (independent of production)
    authorization_status: str
    data_rights_status: str
    rights_scope: RightsScopeOut | None = None
    license_basis: str | None = None
    license_display_name: str | None = None
    rights_display_name: str | None = None
    public_authorization_text: str | None = None
    attribution_required: bool | None = None
    attribution_text_public: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    # Technical availability / quality (separate from rights)
    intended_role: str | None = None
    intended_catalog_scope: str | None = None
    observed_catalog_scope: str
    transport_status: str
    mapper_status: str
    data_quality_status: str
    activation_state: str

    # Coverage (measured; strings)
    price_coverage: str | None = None
    package_quantity_coverage: str | None = None
    package_unit_coverage: str | None = None
    geographic_scope_coverage: str | None = None
    package_coverage: str | None = None
    variable_weight_coverage: str | None = None
    unresolved_costing_coverage: str | None = None
    costing_eligible_product_coverage: str | None = None

    # Costing + production (each its own axis)
    costing_eligibility: str
    production_eligibility: bool
    production_enabled: bool
    production_approved: bool

    # Presentation + metadata
    badge: str
    available_fields: list[str]
    latest_observation_at: datetime | None = None
    metadata_status: str
