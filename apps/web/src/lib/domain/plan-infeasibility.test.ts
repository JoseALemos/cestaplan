import { strict as assert } from "node:assert";
import { test } from "node:test";

import type { InfeasibilityDiagnosis } from "@/lib/api/types";

import { ACTION_CODE_LABELS, INFEASIBILITY_MESSAGES } from "./labels.ts";
import { infeasibilityView } from "./plan-infeasibility.ts";

function diag(overrides: Partial<InfeasibilityDiagnosis> = {}): InfeasibilityDiagnosis {
  return { ...overrides };
}

test("no_active_recipes: deterministic precondition, no retry, translated actions", () => {
  const view = infeasibilityView(
    diag({ code: "no_active_recipes", suggested_actions: ["add_recipes", "change_store"] }),
  );
  assert.equal(view.showBudgetAdjust, false);
  assert.equal(view.canRetry, false);
  assert.ok(view.retryHint !== null && view.retryHint.length > 0);
  assert.equal(view.message, INFEASIBILITY_MESSAGES.no_active_recipes);
  assert.notEqual(view.message, INFEASIBILITY_MESSAGES.genuine_budget_infeasibility);
  for (const action of view.actions) {
    assert.ok(!action.label.includes("_"), `action label leaks underscore: ${action.label}`);
  }
});

test("genuine_budget_infeasibility: budget adjust + retry + minimum budget passthrough", () => {
  const view = infeasibilityView(
    diag({ code: "genuine_budget_infeasibility", minimum_budget: "42.00" }),
  );
  assert.equal(view.showBudgetAdjust, true);
  assert.equal(view.canRetry, true);
  assert.equal(view.retryHint, null);
  assert.equal(view.minimumBudget, "42.00");
});

test("optimizer_error is retryable", () => {
  const view = infeasibilityView(diag({ code: "optimizer_error" }));
  assert.equal(view.canRetry, true);
  assert.equal(view.showBudgetAdjust, false);
});

test("null/undefined diagnosis yields safe defaults", () => {
  for (const input of [null, undefined]) {
    const view = infeasibilityView(input);
    assert.equal(view.canRetry, false);
    assert.equal(view.showBudgetAdjust, false);
    assert.equal(view.minimumBudget, null);
    assert.ok(view.message.length > 0);
    assert.deepEqual(view.actions, []);
  }
});

test("its self-contained copy stays in lockstep with labels.ts", () => {
  // message copy: every PreflightCode renders exactly labels.ts's message.
  for (const [code, message] of Object.entries(INFEASIBILITY_MESSAGES)) {
    assert.equal(infeasibilityView(diag({ code })).message, message);
  }
  // action copy: every ActionCode renders exactly labels.ts's label.
  for (const [code, label] of Object.entries(ACTION_CODE_LABELS)) {
    const view = infeasibilityView(diag({ code: "optimizer_error", suggested_actions: [code] }));
    assert.equal(view.actions[0]?.label, label);
  }
});

test("no rendered action label ever contains an underscore", () => {
  const view = infeasibilityView(
    diag({
      code: "no_costable_recipes",
      suggested_actions: ["review_mappings", "configure_provider", "totally_unknown_code"],
    }),
  );
  assert.ok(view.actions.length > 0);
  for (const action of view.actions) {
    assert.ok(!action.label.includes("_"), `action label leaks underscore: ${action.label}`);
  }
});
