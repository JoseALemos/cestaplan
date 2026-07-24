import { strict as assert } from "node:assert";
import { test } from "node:test";

import { retailerSelectState } from "./retailer-select-state.ts";

test("a successful response with chains is ready", () => {
  assert.equal(
    retailerSelectState({ isSuccess: true, isError: false, optionCount: 8 }),
    "ready",
  );
});

test("only a SUCCESSFUL response with zero chains is empty", () => {
  assert.equal(
    retailerSelectState({ isSuccess: true, isError: false, optionCount: 0 }),
    "empty",
  );
});

test("a failed query is error, never empty", () => {
  assert.equal(
    retailerSelectState({ isSuccess: false, isError: true, optionCount: 0 }),
    "error",
  );
});

test("pending/paused (not success, not error, no data) is loading — never a false 'no chains'", () => {
  // React Query `pending` + `fetchStatus: 'idle'` (offline/paused) reports isLoading=false,
  // isError=false, data=undefined. This is the exact state the old length===0 check mis-rendered
  // as "todavía no hay cadenas" while the catalogue actually had chains.
  assert.equal(
    retailerSelectState({ isSuccess: false, isError: false, optionCount: 0 }),
    "loading",
  );
});

test("an error while a prior success snapshot is still present stays error", () => {
  assert.equal(
    retailerSelectState({ isSuccess: true, isError: true, optionCount: 3 }),
    "error",
  );
});
