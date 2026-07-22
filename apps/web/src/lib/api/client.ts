/**
 * Base HTTP client for the CestaPlan API.
 *
 * - Reads the API origin from `NEXT_PUBLIC_API_BASE_URL`.
 * - Always sends `credentials: "include"` because auth is an **opaque
 *   session cookie** (HttpOnly, Secure in prod), never a JWT in
 *   localStorage — see docs/PRD.md §"Autenticación".
 * - Leaves a named slot for the CSRF header: mutating requests must carry
 *   it once the API contract fixes the header name/token source.
 * - This file intentionally does NOT define data-bound endpoints yet — the
 *   API contract (packages/contracts) isn't wired up. See `endpoints.ts`
 *   for the typed placeholders that will call through `apiFetch`.
 */

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Name of the CSRF header expected by the API on mutating requests. */
export const CSRF_HEADER_NAME = "X-CSRF-Token";

export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  readonly body: unknown;

  constructor(message: string, status: number, url: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
    this.body = body;
  }
}

export interface ApiFetchOptions extends Omit<RequestInit, "body"> {
  /** JSON-serialisable request body. Do not pass raw money values as numbers — money travels as string. */
  body?: unknown;
  /** CSRF token for mutating requests (POST/PUT/PATCH/DELETE). Wired once auth lands. */
  csrfToken?: string;
  /** Abort the request after this many milliseconds. */
  timeoutMs?: number;
}

function isMutatingMethod(method: string | undefined): boolean {
  return ["POST", "PUT", "PATCH", "DELETE"].includes((method ?? "GET").toUpperCase());
}

/**
 * Thin fetch wrapper shared by every future typed endpoint call.
 * Not exported as a hook — pair it with TanStack Query in `useQuery`/
 * `useMutation` at the call site once real endpoints exist.
 */
export async function apiFetch<TResponse>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<TResponse> {
  const { body, csrfToken, timeoutMs = 15_000, headers, method, ...rest } = options;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  const requestHeaders = new Headers(headers);
  if (body !== undefined && !requestHeaders.has("Content-Type")) {
    requestHeaders.set("Content-Type", "application/json");
  }
  if (isMutatingMethod(method) && csrfToken) {
    requestHeaders.set(CSRF_HEADER_NAME, csrfToken);
  }

  const url = `${API_BASE_URL}${path}`;

  try {
    const response = await fetch(url, {
      ...rest,
      method,
      headers: requestHeaders,
      credentials: "include",
      signal: controller.signal,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    const contentType = response.headers.get("content-type") ?? "";
    const payload = contentType.includes("application/json")
      ? await response.json().catch(() => null)
      : await response.text();

    if (!response.ok) {
      throw new ApiError(
        `CestaPlan API respondió ${response.status} para ${path}`,
        response.status,
        url,
        payload,
      );
    }

    return payload as TResponse;
  } finally {
    clearTimeout(timeout);
  }
}
