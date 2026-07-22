/**
 * Base HTTP client for the CestaPlan API.
 *
 * - Reads the API origin from `NEXT_PUBLIC_API_BASE_URL`.
 * - Always sends `credentials: "include"` because auth is an **opaque
 *   session cookie** (HttpOnly, Secure in prod), never a JWT in
 *   localStorage — see docs/PRD.md §"Autenticación".
 * - CSRF: `POST /api/v1/auth/login` sets a non-HttpOnly cookie
 *   `cestaplan_csrf` (readable from JS) and echoes the same value in the
 *   response body as `csrf_token`. Every mutating request (POST/PUT/PATCH/
 *   DELETE) must carry it back in the `X-CSRF-Token` header. By default this
 *   client reads the cookie itself on every mutating call, so callers never
 *   have to thread the token through manually; `csrfToken` is only an
 *   explicit override for edge cases (e.g. immediately after login, before
 *   the cookie write has been observed by `document.cookie` in some browsers).
 */

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Name of the CSRF header expected by the API on mutating requests. */
export const CSRF_HEADER_NAME = "X-CSRF-Token";
/** Name of the readable (non-HttpOnly) cookie the API sets on login. */
export const CSRF_COOKIE_NAME = "cestaplan_csrf";

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
  /** CSRF token override for mutating requests. Defaults to reading the `cestaplan_csrf` cookie. */
  csrfToken?: string;
  /** Abort the request after this many milliseconds. */
  timeoutMs?: number;
}

function isMutatingMethod(method: string | undefined): boolean {
  return ["POST", "PUT", "PATCH", "DELETE"].includes((method ?? "GET").toUpperCase());
}

/** Reads the CSRF token straight from `document.cookie`. Returns `undefined` on the server or if absent. */
export function readCsrfCookie(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${CSRF_COOKIE_NAME}=([^;]*)`),
  );
  return match?.[1] ? decodeURIComponent(match[1]) : undefined;
}

/**
 * Thin fetch wrapper shared by every typed endpoint call in `endpoints.ts`.
 * Not exported as a hook — pair it with TanStack Query in `useQuery`/
 * `useMutation` at the call site.
 */
export async function apiFetch<TResponse>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<TResponse> {
  const { body, csrfToken, timeoutMs = 20_000, headers, method, ...rest } = options;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  const requestHeaders = new Headers(headers);
  if (body !== undefined && !requestHeaders.has("Content-Type")) {
    requestHeaders.set("Content-Type", "application/json");
  }
  if (isMutatingMethod(method)) {
    const token = csrfToken ?? readCsrfCookie();
    if (token) {
      requestHeaders.set(CSRF_HEADER_NAME, token);
    }
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

    if (response.status === 204) {
      return undefined as TResponse;
    }

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
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(`Tiempo de espera agotado para ${path}`, 0, url, null);
    }
    throw new ApiError(
      `No se pudo conectar con la API de CestaPlan (${path})`,
      0,
      url,
      error instanceof Error ? error.message : error,
    );
  } finally {
    clearTimeout(timeout);
  }
}
