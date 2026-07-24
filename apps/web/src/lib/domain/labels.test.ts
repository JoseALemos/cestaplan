import { strict as assert } from "node:assert";
import { test } from "node:test";

import {
  ACTION_CODE_LABELS,
  INFEASIBILITY_MESSAGES,
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
