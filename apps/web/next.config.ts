import type { NextConfig } from "next";
import path from "node:path";

import { API_PROXY_PREFIX, resolveApiUpstream } from "./src/lib/proxy/upstream";

// Server-only: the API upstream the /api-proxy rewrite forwards to. Never exposed to the browser
// (the browser sees only NEXT_PUBLIC_API_BASE_URL=/api-proxy). Validated + normalized at build.
const API_UPSTREAM_URL = resolveApiUpstream(process.env.API_UPSTREAM_URL);

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle (`.next/standalone`) for the Docker image.
  // The runner then only needs Node + `apps/web/server.js`, not the full node_modules.
  output: "standalone",
  // This is a pnpm monorepo: pin the file-tracing root to the repo root so the
  // standalone build traces workspace files deterministically regardless of CWD.
  outputFileTracingRoot: path.join(__dirname, "../../"),
  async rewrites() {
    // Same-origin proxy: the browser hits `${API_PROXY_PREFIX}/api/v1/...` on the web origin and
    // Next forwards it to `${API_UPSTREAM_URL}/api/v1/...` server-side, preserving method, query,
    // body (JSON + multipart), Cookie/Set-Cookie, Content-Type and status (incl. 204). This keeps
    // the session/CSRF cookies first-party to the web host without weakening CSRF or CORS.
    return [
      {
        source: `${API_PROXY_PREFIX}/:path*`,
        destination: `${API_UPSTREAM_URL}/:path*`,
      },
    ];
  },
};

export default nextConfig;
