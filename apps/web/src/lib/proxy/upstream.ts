/**
 * Same-origin API proxy configuration for the Next.js web service.
 *
 * The browser only ever talks to the web origin at `${API_PROXY_PREFIX}/...`. Next.js rewrites
 * that, server-side, to `${upstream}/...`, so the API's `Set-Cookie` (session + CSRF) is received
 * by the browser as a **first-party** cookie of the web host — no cross-site third-party cookie,
 * no CSRF/ CORS relaxation. The upstream URL is a **server-only** variable: it never reaches the
 * browser bundle (only `NEXT_PUBLIC_API_BASE_URL=/api-proxy` does).
 */

/** Path prefix the browser uses for the same-origin API proxy. */
export const API_PROXY_PREFIX = "/api-proxy";

export interface ResolveUpstreamOptions {
  /** Overrides `process.env.NODE_ENV`; production requires an explicit upstream. */
  nodeEnv?: string;
}

/**
 * Resolve + validate `API_UPSTREAM_URL` for the `/api-proxy` rewrite. Normalizes the trailing
 * slash, requires a value in production, rejects a non-absolute/ non-http(s) URL, and refuses a
 * value that points back at the proxy path (which would loop). Falls back to `http://localhost:8000`
 * only outside production (local dev / tests).
 */
export function resolveApiUpstream(
  raw: string | undefined,
  opts: ResolveUpstreamOptions = {},
): string {
  const nodeEnv = opts.nodeEnv ?? process.env.NODE_ENV;
  const isProd = nodeEnv === "production";
  const value = (raw ?? "").trim();

  if (!value) {
    if (isProd) {
      throw new Error(
        "API_UPSTREAM_URL is required in production: the web service proxies /api-proxy to it.",
      );
    }
    return "http://localhost:8000";
  }

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`API_UPSTREAM_URL must be an absolute http(s) URL (got: ${value}).`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`API_UPSTREAM_URL must use http(s) (got protocol: ${url.protocol}).`);
  }
  // Refuse an upstream that points back at the proxy path — that would loop the web onto itself.
  if (url.pathname.replace(/\/+$/, "").endsWith(API_PROXY_PREFIX)) {
    throw new Error("API_UPSTREAM_URL must not point back at the proxy path (would loop).");
  }
  // Strip trailing slash(es) so `${upstream}/:path*` never produces a double slash.
  return value.replace(/\/+$/, "");
}
