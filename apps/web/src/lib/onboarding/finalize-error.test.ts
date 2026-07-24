import { strict as assert } from "node:assert";
import { test } from "node:test";

import { finalizeErrorMessage, finalizeErrorNeedsLogin } from "./finalize-error.ts";

test("401 asks the user to sign in", () => {
  assert.match(finalizeErrorMessage(401), /iniciar sesión/i);
  assert.equal(finalizeErrorNeedsLogin(401), true);
});

test("403 explains the cross-domain cookie cause and offers re-login (no internals)", () => {
  const msg = finalizeErrorMessage(403);
  assert.match(msg, /no pudimos verificar tu sesión/i);
  assert.match(msg, /mismo dominio/i);
  // Never leaks token/CSRF wording or a literal cookie value.
  assert.doesNotMatch(msg, /token|csrf|cookie=/i);
  assert.equal(finalizeErrorNeedsLogin(403), true);
});

test("422 points at the entered data", () => {
  assert.match(finalizeErrorMessage(422), /datos/i);
  assert.equal(finalizeErrorNeedsLogin(422), false);
});

test("other API errors are retryable and idempotent-safe", () => {
  assert.match(finalizeErrorMessage(500), /\(500\)/);
  assert.match(finalizeErrorMessage(500), /no se repetirá/i);
  assert.equal(finalizeErrorNeedsLogin(500), false);
});

test("a network (non-API) error suggests checking the connection", () => {
  assert.match(finalizeErrorMessage(null), /conexión|conectar/i);
  assert.equal(finalizeErrorNeedsLogin(null), false);
});
