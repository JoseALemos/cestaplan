"""Canonical, code-level declaration of each price source's authorized rights.

The project owner has confirmed, via **private commercial agreements**, a valid API licence and
the chain owner's express authorization to store, process, display and commercially use the data
of DIA, Alcampo, Carrefour, Lidl, Aldi, Deza and Mercadona inside CestaPlan. Those agreements are
confidential: this module records only the **public-safe** facts (that the use is authorized, the
licence basis, a public authorization text and the explicit scope of permissions). It never
contains contract references, signatory names, expedient numbers or any secret — those live only
in the internal, admin-only fields on ``ProviderActivation`` and are set out-of-band.

This registry is the single source of truth used by:
  * ``tools.bootstrap_source_rights`` — writes these facts into ``ProviderActivation`` rows;
  * ``routers.catalog.list_price_providers`` — derives technical facts + rights display;
so the authorized state is declared exactly once.

Design rules honoured here:
  * legal rights are kept **separate** from production activation — nothing here enables the
    planner, sync, or costing;
  * ``raw_redistribution`` stays ``False`` unless a differentiated authorization exists;
  * ``attribution_required = None`` means "governed by a private agreement" — NOT a claim that
    attribution is unnecessary;
  * an intermediary technical provider (Parse.bot / Apify) is never presented as an official API.
"""

from __future__ import annotations

from dataclasses import dataclass

# Public authorization texts (safe to display; never reveal contract terms).
PRIVATE_COMMERCIAL_AUTH_TEXT = (
    "Datos utilizados con licencia de la API y autorización del titular de la cadena para su "
    "almacenamiento, tratamiento, visualización y uso en CestaPlan. Los términos contractuales "
    "completos son confidenciales."
)
ODBL_AUTH_TEXT = (
    "Datos de la comunidad Open Food Facts - Open Prices, disponibles bajo licencia ODbL. Se "
    "conserva la atribución oficial que la licencia requiere."
)
DEMO_AUTH_TEXT = (
    "Catálogo sintético propio (MercaEjemplo) para demostración y desarrollo. No representa "
    "precios reales de ninguna cadena."
)

# Display names (public-safe).
LICENSE_DISPLAY_PRIVATE = "Licencia comercial privada"
LICENSE_DISPLAY_ODBL = "ODbL (licencia de base de datos abierta)"
LICENSE_DISPLAY_OWN = "Datos sintéticos propios"
RIGHTS_DISPLAY_AUTHORIZED = "Uso autorizado"
RIGHTS_DISPLAY_ODBL = "Datos abiertos comunitarios"
RIGHTS_DISPLAY_OWN = "Datos de demostración"


def _private_commercial_scope() -> dict[str, object | None]:
    """Scope for a private commercial agreement: full authorized use, raw redistribution off,
    attribution governed by the (private) agreement (``None``, not "not required")."""
    return {
        "api_access": True,
        "storage": True,
        "processing": True,
        "display": True,
        "commercial_use": True,
        "derived_results": True,
        "raw_redistribution": False,
        "attribution_required": None,
    }


def _odbl_scope() -> dict[str, object | None]:
    """Open Prices under ODbL: open data, attribution IS required, redistribution allowed."""
    return {
        "api_access": True,
        "storage": True,
        "processing": True,
        "display": True,
        "commercial_use": True,
        "derived_results": True,
        "raw_redistribution": True,
        "attribution_required": True,
    }


def _own_synthetic_scope() -> dict[str, object | None]:
    """Our own synthetic demo data: fully ours; no external API; no attribution obligation."""
    return {
        "api_access": False,
        "storage": True,
        "processing": True,
        "display": True,
        "commercial_use": True,
        "derived_results": True,
        "raw_redistribution": True,
        "attribution_required": False,
    }


@dataclass(frozen=True, slots=True)
class SourceRights:
    """Public-safe, canonical rights + technical facts for one price source."""

    provider_code: str
    provider_display_name: str
    retailer_display_name: str
    # Intermediary technical provider (Parse.bot / Apify) — ``None`` when the source is direct.
    technical_provider: str | None
    # True ONLY for a genuine official API of the data owner. An intermediary scraper/agent is
    # never "official", even when the chain owner authorized the data use.
    official_api: bool
    source_type: str
    source_url: str | None
    data_rights_status: str
    authorization_status: str
    license_basis: str
    license_display_name: str
    rights_display_name: str
    public_authorization_text: str
    attribution_text_public: str | None
    rights_scope: dict[str, object | None]

    @property
    def authorized_source(self) -> bool:
        """The owner has confirmed a rights basis for using this source (not under review)."""
        return self.authorization_status == "verified"


def _parsebot(code: str, retailer_display: str, source_url: str | None) -> SourceRights:
    return SourceRights(
        provider_code=code,
        provider_display_name=f"{retailer_display} (vía Parse.bot)",
        retailer_display_name=retailer_display,
        technical_provider="Parse.bot",
        official_api=False,
        source_type="authorized_partner",
        source_url=source_url,
        data_rights_status="commercial_use_allowed",
        authorization_status="verified",
        license_basis="private_commercial_agreement",
        license_display_name=LICENSE_DISPLAY_PRIVATE,
        rights_display_name=RIGHTS_DISPLAY_AUTHORIZED,
        public_authorization_text=PRIVATE_COMMERCIAL_AUTH_TEXT,
        attribution_text_public=None,
        rights_scope=_private_commercial_scope(),
    )


SOURCE_RIGHTS: dict[str, SourceRights] = {
    "parsebot-dia": _parsebot("parsebot-dia", "DIA", "https://www.dia.es"),
    "parsebot-alcampo": _parsebot("parsebot-alcampo", "Alcampo", "https://www.alcampo.es"),
    "parsebot-carrefour": _parsebot(
        "parsebot-carrefour", "Carrefour", "https://www.carrefour.es"
    ),
    "parsebot-lidl": _parsebot("parsebot-lidl", "Lidl", "https://www.lidl.es"),
    "parsebot-aldi": _parsebot("parsebot-aldi", "Aldi", "https://www.aldi.es"),
    "parsebot-deza": _parsebot("parsebot-deza", "Deza", None),
    "apify-mercadona": SourceRights(
        provider_code="apify-mercadona",
        provider_display_name="Mercadona (vía Apify)",
        retailer_display_name="Mercadona",
        technical_provider="Apify",
        official_api=False,
        source_type="authorized_partner",
        source_url="https://www.mercadona.es",
        data_rights_status="commercial_use_allowed",
        authorization_status="verified",
        license_basis="private_commercial_agreement",
        license_display_name=LICENSE_DISPLAY_PRIVATE,
        rights_display_name=RIGHTS_DISPLAY_AUTHORIZED,
        public_authorization_text=PRIVATE_COMMERCIAL_AUTH_TEXT,
        attribution_text_public=None,
        rights_scope=_private_commercial_scope(),
    ),
    "open-prices": SourceRights(
        provider_code="open-prices",
        provider_display_name="Open Prices",
        retailer_display_name="Open Prices",
        technical_provider=None,
        official_api=True,
        source_type="open_dataset",
        source_url="https://prices.openfoodfacts.org",
        data_rights_status="odbl",
        authorization_status="verified",
        license_basis="odbl",
        license_display_name=LICENSE_DISPLAY_ODBL,
        rights_display_name=RIGHTS_DISPLAY_ODBL,
        public_authorization_text=ODBL_AUTH_TEXT,
        attribution_text_public=(
            "© Open Food Facts - Open Prices, bajo licencia ODbL."
        ),
        rights_scope=_odbl_scope(),
    ),
    "demo": SourceRights(
        provider_code="demo",
        provider_display_name="MercaEjemplo (demostración)",
        retailer_display_name="MercaEjemplo",
        technical_provider=None,
        official_api=False,
        source_type="demo",
        source_url=None,
        data_rights_status="own_synthetic",
        authorization_status="verified",
        license_basis="own_synthetic",
        license_display_name=LICENSE_DISPLAY_OWN,
        rights_display_name=RIGHTS_DISPLAY_OWN,
        public_authorization_text=DEMO_AUTH_TEXT,
        attribution_text_public=(
            "Datos sintéticos de ejemplo. No son precios reales."
        ),
        rights_scope=_own_synthetic_scope(),
    ),
}

# The seven externally-owned chains authorized via private commercial agreements.
AUTHORIZED_EXTERNAL_CODES = (
    "parsebot-dia",
    "parsebot-alcampo",
    "parsebot-carrefour",
    "parsebot-lidl",
    "parsebot-aldi",
    "parsebot-deza",
    "apify-mercadona",
)


def get_source_rights(provider_code: str) -> SourceRights | None:
    return SOURCE_RIGHTS.get(provider_code)


__all__ = [
    "AUTHORIZED_EXTERNAL_CODES",
    "SOURCE_RIGHTS",
    "SourceRights",
    "get_source_rights",
]
