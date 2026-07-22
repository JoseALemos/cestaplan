import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle (`.next/standalone`) for the Docker image.
  // The runner then only needs Node + `apps/web/server.js`, not the full node_modules.
  output: "standalone",
  // This is a pnpm monorepo: pin the file-tracing root to the repo root so the
  // standalone build traces workspace files deterministically regardless of CWD.
  outputFileTracingRoot: path.join(__dirname, "../../"),
};

export default nextConfig;
