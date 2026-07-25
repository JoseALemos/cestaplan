import { strict as assert } from "node:assert";
import { test } from "node:test";

import {
  ACTION_CODE_LABELS,
  INFEASIBILITY_MESSAGES,
  isBackendPriceCoverageWarning,
  PRICE_COVERAGE_NOTICE,
  priceCoverageState,
  READINESS_STATUS_LABELS,
  actionLabel,
  infeasibilityMessage,
  readinessStatusLabel,
} from "./labels.ts";

test("every ActionCode maps to a non-empty label with no underscore", () => {
  for (const [code, label] of Object.entries(ACTION_CODE_LABELS)) {
    assert.ok(label.length > 0, `${code} has an empty label`);
    assert.ok(!label.includes("_"), `${code} label leaks an underscore: ${label}`);
  }
});

test("actionLabel falls back to a neutral label for unknown codes — never the slug", () => {
  const fallback = actionLabel("some_unknown_code");
  assert.equal(fallback, "Ajuste sugerido");
  assert.ok(!fallback.includes("_"));
  assert.notEqual(fallback, "some_unknown_code");
});

test("every PreflightCode maps to a non-empty message", () => {
  for (const [code, message] of Object.entries(INFEASIBILITY_MESSAGES)) {
    assert.ok(message.length > 0, `${code} has an empty message`);
  }
});

test("infeasibilityMessage returns the mapped copy for a known code", () => {
  assert.equal(
    infeasibilityMessage("no_active_recipes"),
    INFEASIBILITY_MESSAGES.no_active_recipes,
  );
});

test("infeasibilityMessage uses the fallback for unknown codes", () => {
  assert.equal(infeasibilityMessage("mystery", "copia de reserva"), "copia de reserva");
  assert.ok(infeasibilityMessage(undefined).length > 0);
});

test("every ReadinessStatus maps to a non-empty label", () => {
  for (const [status, label] of Object.entries(READINESS_STATUS_LABELS)) {
    assert.ok(label.length > 0, `${status} has an empty label`);
  }
});

test("readinessStatusLabel falls back neutrally for unknown status", () => {
  assert.equal(readinessStatusLabel("weird_status"), "Estado desconocido");
});

test("priceCoverageState: no prices at all -> none (by status or 0 ratio)", () => {
  assert.equal(priceCoverageState({ status: "none", price_coverage: "0" }), "none");
  assert.equal(priceCoverageState({ status: "sin_datos" }), "none");
  assert.equal(priceCoverageState({ status: "no_data" }), "none");
  assert.equal(priceCoverageState({ price_coverage: "0" }), "none");
  assert.equal(priceCoverageState({ price_coverage: "0.0000" }), "none");
});

test("priceCoverageState: some but not all prices -> partial", () => {
  assert.equal(priceCoverageState({ status: "partial", price_coverage: "0.5" }), "partial");
  assert.equal(priceCoverageState({ status: "cobertura_parcial" }), "partial");
  assert.equal(priceCoverageState({ status: "cobertura_insuficiente" }), "partial");
  assert.equal(priceCoverageState({ status: "datos_caducados" }), "partial");
  assert.equal(priceCoverageState({ price_coverage: "0.42" }), "partial");
});

test("priceCoverageState: full/high coverage or unknown -> ok", () => {
  assert.equal(priceCoverageState({ status: "complete", price_coverage: "1.0000" }), "ok");
  assert.equal(priceCoverageState({ status: "cobertura_alta", price_coverage: "1" }), "ok");
  assert.equal(priceCoverageState(null), "ok");
  assert.equal(priceCoverageState(undefined), "ok");
  assert.equal(priceCoverageState({}), "ok");
});

test("priceCoverageState: a 0 ratio means none even with a partial-looking status", () => {
  assert.equal(priceCoverageState({ status: "cobertura_parcial", price_coverage: "0" }), "none");
});

test("isBackendPriceCoverageWarning: matches the engine's English coverage warning", () => {
  assert.equal(
    isBackendPriceCoverageWarning("price coverage is low; total cost is not reliable"),
    true,
  );
  assert.equal(isBackendPriceCoverageWarning("PRICE COVERAGE IS LOW"), true);
  assert.equal(isBackendPriceCoverageWarning("total cost is not reliable"), true);
  assert.equal(isBackendPriceCoverageWarning("nutrición incompleta en 2 platos"), false);
});

test("PRICE_COVERAGE_NOTICE: localized, non-empty copy for the two unreliable states", () => {
  for (const state of ["none", "partial"] as const) {
    const notice = PRICE_COVERAGE_NOTICE[state];
    assert.ok(notice.title.length > 0);
    assert.ok(notice.body.length > 0);
    assert.notEqual(notice.title, state);
  }
  assert.equal(PRICE_COVERAGE_NOTICE.none.tone, "info");
  assert.equal(PRICE_COVERAGE_NOTICE.partial.tone, "warning");
});
