import { strict as assert } from "node:assert";
import { test } from "node:test";

import { buildRequestUrl } from "./client.ts";

test("with the same-origin proxy base, a call stays a relative /api-proxy path", () => {
  // apiFetch("/api/v1/households") with NEXT_PUBLIC_API_BASE_URL=/api-proxy
  assert.equal(buildRequestUrl("/api-proxy", "/api/v1/households"), "/api-proxy/api/v1/households");
  // The result is relative (same-origin) — never an absolute cross-origin URL.
  assert.doesNotMatch(buildRequestUrl("/api-proxy", "/api/v1/households"), /^https?:/);
});

test("path and query string are preserved verbatim", () => {
  assert.equal(
    buildRequestUrl("/api-proxy", "/api/v1/retailers/x/stores/y/prices?search=leche&page=2"),
    "/api-proxy/api/v1/retailers/x/stores/y/prices?search=leche&page=2",
  );
});

test("an absolute base (bare local run) still composes correctly", () => {
  assert.equal(
    buildRequestUrl("http://localhost:8000", "/api/v1/households"),
    "http://localhost:8000/api/v1/households",
  );
});
