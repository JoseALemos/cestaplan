# CestaPlan web image (Next.js 16, standalone output, pnpm workspace).
#
# Build context = repository ROOT (matches docker-compose.yml and infra/railway/web.json):
#   docker build -f apps/web/Dockerfile \
#     --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com \
#     -t cestaplan-web .
#
# NOTE: NEXT_PUBLIC_* values are BAKED IN AT BUILD TIME (inlined into the client
# bundle). To change NEXT_PUBLIC_API_BASE_URL you must rebuild the image.

# ---------- Base: Node 22 + pnpm via corepack ----------
FROM node:22-alpine AS base
ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:$PATH" \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10.30.3 --activate
WORKDIR /app

# ---------- Dependencies: install from the workspace lockfile ----------
FROM base AS deps
# Only the manifests needed to resolve the lockfile (best layer caching).
# packages/* are empty scaffolds (no package.json), so only web is installed.
COPY pnpm-lock.yaml pnpm-workspace.yaml package.json ./
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile

# ---------- Builder: produce the standalone server ----------
FROM base AS builder
# Public API base URL is compiled into the client bundle here.
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/web/node_modules ./apps/web/node_modules
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json ./
COPY apps/web ./apps/web
RUN pnpm --filter @cestaplan/web build

# ---------- Runner: minimal, non-root ----------
FROM base AS runner
ENV NODE_ENV=production \
    PORT=3000
# node:*-alpine ships a `node` user (uid 1000); run as it, no shell tooling needed.
WORKDIR /app

# Standalone output mirrors the monorepo layout under .next/standalone:
#   - server entry:   apps/web/server.js
#   - static assets:  apps/web/.next/static
#   - public assets:  apps/web/public
COPY --from=builder --chown=node:node /app/apps/web/.next/standalone ./
COPY --from=builder --chown=node:node /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=node:node /app/apps/web/public ./apps/web/public

USER node

EXPOSE 3000

# Railway/compose inject $PORT; Next's standalone server reads it from the env.
CMD ["node", "apps/web/server.js"]
