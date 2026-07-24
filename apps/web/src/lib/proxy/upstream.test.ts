import { strict as assert } from "node:assert";
import { test } from "node:test";

import { API_PROXY_PREFIX, resolveApiUpstream } from "./upstream.ts";

test("the browser-facing proxy prefix is /api-proxy", () => {
  assert.equal(API_PROXY_PREFIX, "/api-proxy");
});

test("a valid upstream keeps its origin and drops a trailing slash", () => {
  assert.equal(
    resolveApiUpstream("https://api-production-4c5d.up.railway.app/", { nodeEnv: "production" }),
    "https://api-production-4c5d.up.railway.app",
  );
  assert.equal(
    resolveApiUpstream("https://api.example.com", { nodeEnv: "production" }),
    "https://api.example.com",
  );
});

test("an empty upstream is required in production", () => {
  assert.throws(() => resolveApiUpstream("", { nodeEnv: "production" }), /required in production/);
  assert.throws(
    () => resolveApiUpstream(undefined, { nodeEnv: "production" }),
    /required in production/,
  );
});

test("dev/test fall back to localhost:8000", () => {
  assert.equal(resolveApiUpstream("", { nodeEnv: "development" }), "http://localhost:8000");
  assert.equal(resolveApiUpstream(undefined, { nodeEnv: "test" }), "http://localhost:8000");
});

test("localhost is allowed as an explicit upstream", () => {
  assert.equal(
    resolveApiUpstream("http://localhost:8000", { nodeEnv: "development" }),
    "http://localhost:8000",
  );
});

test("a non-absolute or non-http(s) upstream is rejected", () => {
  assert.throws(() => resolveApiUpstream("/api-proxy", { nodeEnv: "production" }), /absolute/);
  assert.throws(() => resolveApiUpstream("api.example.com", { nodeEnv: "production" }), /absolute/);
  assert.throws(() => resolveApiUpstream("ftp://x.example.com", { nodeEnv: "production" }), /http/);
});

test("an upstream pointing back at the proxy path is rejected (no loop)", () => {
  assert.throws(
    () => resolveApiUpstream("https://web.example.com/api-proxy", { nodeEnv: "production" }),
    /loop/,
  );
  assert.throws(
    () => resolveApiUpstream("https://web.example.com/api-proxy/", { nodeEnv: "production" }),
    /loop/,
  );
});
