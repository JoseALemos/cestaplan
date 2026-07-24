import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CATEGORY_LABELS,
  formatCategoryLabel,
  formatNormalizedUnitPrice,
  formatPackagePrice,
  formatPurchaseLine,
  formatRequiredQuantity,
  formatSourceLabel,
} from "./shopping-format.ts";

// Intl inserts narrow/no-break spaces around units and currency; normalize for robust asserts.
const norm = (s: string) => s.replace(/[  ]/g, " ");

test("formatRequiredQuantity shows the unit and normalizes readably", () => {
  assert.equal(norm(formatRequiredQuantity("109.5", "ml")), "109,5 ml");
  assert.equal(norm(formatRequiredQuantity("600", "g")), "600 g");
  assert.equal(norm(formatRequiredQuantity("1500", "g")), "1,5 kg"); // >1kg -> kg
  assert.equal(norm(formatRequiredQuantity("2000", "ml")), "2 l"); // >1l -> l
  assert.equal(norm(formatRequiredQuantity("12", "unit")), "12 ud"); // discrete
  assert.equal(norm(formatRequiredQuantity("3", "ud")), "3 ud");
});

test("formatRequiredQuantity handles null/empty", () => {
  assert.equal(formatRequiredQuantity(null, "g"), "—");
  assert.equal(formatRequiredQuantity(undefined, "g"), "—");
  assert.equal(formatRequiredQuantity("", "g"), "—");
});

test("formatPackagePrice is the whole-package price, never a per-gram value", () => {
  assert.equal(norm(formatPackagePrice("3.19")), "3,19 €/envase");
  assert.equal(norm(formatPackagePrice("0.81")), "0,81 €/envase");
  assert.equal(formatPackagePrice(null), "—");
  // The old bug: a per-gram value rounded to cents. It must never be produced here.
  assert.notEqual(norm(formatPackagePrice("3.19")), "0,01 €/envase");
});

test("formatPurchaseLine distinguishes single vs multiple packages", () => {
  assert.equal(norm(formatPurchaseLine("1.62", 2)), "2 envases · 1,62 €");
  assert.equal(norm(formatPurchaseLine("3.19", 1)), "3,19 €");
  assert.equal(norm(formatPurchaseLine("3.19", null)), "3,19 €");
  assert.equal(formatPurchaseLine(null, 2), "—");
});

test("formatNormalizedUnitPrice renders €/kg, €/l, €/unidad", () => {
  assert.equal(norm(formatNormalizedUnitPrice("6.38", "l")), "6,38 €/l");
  assert.equal(norm(formatNormalizedUnitPrice("5.94", "kg")), "5,94 €/kg");
  assert.equal(norm(formatNormalizedUnitPrice("0.17", "unidad")), "0,17 €/unidad");
  assert.equal(formatNormalizedUnitPrice(null, "kg"), "—");
  assert.equal(formatNormalizedUnitPrice("6.38", null), "—");
});

test("formatSourceLabel is text-first (never color-only) and labels demo honestly", () => {
  assert.equal(
    formatSourceLabel("demo", "MercaEjemplo demo", "2026-07-21T00:00:00Z").startsWith(
      "Precio demo · MercaEjemplo demo",
    ),
    true,
  );
  assert.equal(formatSourceLabel("confirmed_external", "Alcampo", null), "Precio confirmado · Alcampo");
  assert.equal(formatSourceLabel("estimated", null, null), "Precio estimado");
  assert.equal(formatSourceLabel("unavailable", null, null), "Sin precio");
  assert.equal(formatSourceLabel(null, null, null), "Sin precio");
});

test("formatCategoryLabel maps every known slug + degrades gracefully", () => {
  assert.equal(formatCategoryLabel("aceites_condimentos"), "Aceites y condimentos");
  assert.equal(formatCategoryLabel("cereales_pasta_arroz"), "Cereales, pasta y arroz");
  assert.equal(formatCategoryLabel("lacteos"), "Lácteos");
  assert.equal(formatCategoryLabel("uncategorized"), "Sin categoría");
  assert.equal(formatCategoryLabel(null), "Sin categoría");
  assert.equal(formatCategoryLabel("nuevo_slug"), "Nuevo Slug"); // unknown -> de-slugged, never raw
  // Every documented category has a label.
  for (const slug of [
    "carne",
    "conservas_despensa",
    "frutas",
    "frutos_secos_semillas",
    "huevos",
    "legumbres",
    "panaderia",
    "pescado_marisco",
    "verduras",
  ]) {
    assert.ok(CATEGORY_LABELS[slug], `missing label for ${slug}`);
  }
});
