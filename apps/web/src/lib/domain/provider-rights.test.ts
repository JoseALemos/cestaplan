import { strict as assert } from "node:assert";
import { test } from "node:test";

import type { PriceProvider } from "@/lib/api/types";

import { providerAuthorizationView, providerAxes } from "./provider-rights.ts";

function base(overrides: Partial<PriceProvider> = {}): PriceProvider {
  return {
    provider: "parsebot-dia",
    retailer: "dia",
    retailer_id: null,
    intended_role: "dense_candidate",
    intended_catalog_scope: "full",
    observed_catalog_scope: "unknown",
    price_coverage: null,
    package_quantity_coverage: null,
    package_unit_coverage: null,
    geographic_scope_coverage: null,
    package_coverage: null,
    variable_weight_coverage: null,
    unresolved_costing_coverage: null,
    costing_eligible_product_coverage: null,
    costing_eligibility: "unknown",
    production_eligibility: false,
    activation_state: "disabled",
    transport_status: "unknown",
    mapper_status: "unknown",
    data_rights_status: "commercial_use_allowed",
    badge: "Configuración pendiente",
    ...overrides,
  };
}

test("an authorized private-commercial source shows the authorized labels", () => {
  const v = providerAuthorizationView(
    base({
      authorized_source: true,
      authorization_status: "verified",
      license_display_name: "Licencia comercial privada",
      rights_display_name: "Uso autorizado",
      technical_provider: "Parse.bot",
      official_api: false,
      attribution_required: null,
    }),
  );
  assert.equal(v.licenseLabel, "Licencia comercial privada");
  assert.equal(v.rightsLabel, "Uso autorizado");
  assert.equal(v.isAuthorized, true);
  assert.equal(v.isOfficialApi, false);
  assert.equal(v.technicalProvider, "Parse.bot");
  assert.equal(v.attributionRequired, null); // private agreement — NOT "not required"
});

test("Open Prices is an official API and requires attribution", () => {
  const v = providerAuthorizationView(
    base({
      provider: "open-prices",
      authorized_source: true,
      official_api: true,
      technical_provider: null,
      attribution_required: true,
      license_display_name: "ODbL (licencia de base de datos abierta)",
      rights_display_name: "Datos abiertos comunitarios",
    }),
  );
  assert.equal(v.isOfficialApi, true);
  assert.equal(v.attributionRequired, true);
  assert.equal(v.technicalProvider, null);
});

test("a version-lagged API missing the rights block never shows 'no licence' copy", () => {
  const v = providerAuthorizationView(base({ authorization_status: undefined }));
  // Honest neutral fallback — NEVER the forbidden "Licencia no especificada".
  assert.equal(v.licenseLabel, "Licencia por confirmar");
  assert.notEqual(v.licenseLabel, "Licencia no especificada");
  assert.equal(v.isAuthorized, false);
  assert.equal(v.rightsLabel, "Pendiente de revisión");
});

test("axes stay independent: authorized but not costable is experimental, not blocked", () => {
  const a = providerAxes(
    base({ authorized_source: true, costing_eligibility: "insufficient" }),
  );
  assert.equal(a.authorized, true);
  assert.equal(a.costable, false);
  assert.equal(a.experimental, true);
});

test("planner approval requires BOTH production flags — rights alone never grant it", () => {
  assert.equal(
    providerAxes(base({ authorized_source: true, production_enabled: true })).productionApproved,
    false,
  );
  assert.equal(
    providerAxes(
      base({ authorized_source: true, production_enabled: true, production_approved: true }),
    ).productionApproved,
    true,
  );
});

test("a costable source reports costable and not experimental", () => {
  const a = providerAxes(
    base({ authorized_source: true, costing_eligibility: "sufficient", transport_status: "operational" }),
  );
  assert.equal(a.costable, true);
  assert.equal(a.experimental, false);
  assert.equal(a.operational, true);
});
