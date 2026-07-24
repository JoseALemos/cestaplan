import type { PriceProvider } from "@/lib/api/types";

/**
 * Presentation of a price source's LEGAL rights — a distinct axis from technical availability,
 * costing eligibility and production activation. Derived defensively so a version-lagged API that
 * omits the rights block never shows "unknown licence": we fall back to honest neutral copy, never
 * to "Licencia no especificada" / "Sin autorización" for a source the backend already governs.
 */
export interface ProviderAuthorizationView {
  licenseLabel: string;
  rightsLabel: string;
  isAuthorized: boolean;
  isOfficialApi: boolean;
  technicalProvider: string | null;
  publicText: string | null;
  /** null = governed by a private agreement (NOT "attribution not required"). */
  attributionRequired: boolean | null;
  attributionText: string | null;
}

export function providerAuthorizationView(p: PriceProvider): ProviderAuthorizationView {
  const isAuthorized = p.authorized_source ?? p.authorization_status === "verified";
  return {
    licenseLabel: p.license_display_name ?? "Licencia por confirmar",
    rightsLabel: p.rights_display_name ?? (isAuthorized ? "Uso autorizado" : "Pendiente de revisión"),
    isAuthorized,
    isOfficialApi: p.official_api ?? false,
    technicalProvider: p.technical_provider ?? null,
    publicText: p.public_authorization_text ?? null,
    attributionRequired: p.attribution_required ?? null,
    attributionText: p.attribution_text_public ?? null,
  };
}

/**
 * The five independent axes the UI must never collapse into one label (spec §9): a source can be
 * legally authorized yet not operational, operational yet not costable, costable yet not approved
 * for the planner. Each is reported separately.
 */
export interface ProviderAxes {
  authorized: boolean;
  operational: boolean;
  costable: boolean;
  experimental: boolean;
  productionApproved: boolean;
}

export function providerAxes(p: PriceProvider): ProviderAxes {
  const authorized = p.authorized_source ?? p.authorization_status === "verified";
  const costable = p.costing_eligibility === "sufficient";
  const operational = p.transport_status === "operational";
  return {
    authorized,
    operational,
    costable,
    // Authorized but not yet costable → experimental (never "legally blocked").
    experimental: authorized && !costable,
    // The planner uses a source ONLY when BOTH flags are set (never granted by rights alone).
    productionApproved: Boolean(p.production_enabled) && Boolean(p.production_approved),
  };
}
