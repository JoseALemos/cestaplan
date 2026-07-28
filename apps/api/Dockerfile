# CestaPlan backend image (FastAPI api + queue worker + Alembic).
#
# Build context = repository ROOT (matches docker-compose.yml and infra/railway/*.json):
#   docker build -f apps/api/Dockerfile -t cestaplan-api .
#
# One image, three entrypoints (override the command per service):
#   api     -> uvicorn cestaplan_api.main:app --host 0.0.0.0 --port $PORT   (default CMD)
#   worker  -> python -m cestaplan_worker.main
#   migrate -> alembic upgrade head   (Railway pre-deploy command / compose)

# ---------- Shared base: python + pinned PostgreSQL 18 CLIENT tools (no server) ----------
# fix(runtime): BackupEvidence executes pg_restore over /proc/self/fd/<fd>; the image must ship the
# pg_restore / pg_dump CLIENT binaries at the exact MAJOR 18. Installed from the OFFICIAL PostgreSQL
# Apt Repository (apt.postgresql.org) with the official signing key, pinned to an EXACT version — no
# floating tag, no server, no fallback major. Both the builder (so the provenance generator measures
# the very binaries that ship) and the runtime inherit this stage, so they contain identical tools.
# Toolchain pinned by tag + digest (immutable-build-provenance): these digests MUST match the
# constants in cestaplan_api/provenance/generator.py exactly (CI asserts equality; forbids a floating
# tag or a digest-less FROM). Never float the base image, uv or the PostgreSQL client version.
ARG POSTGRESQL_CLIENT_18_VERSION="18.4-1.pgdg13+1"
ARG PGDG_KEY_SHA256="0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76"

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS pg-client-base
ARG POSTGRESQL_CLIENT_18_VERSION
ARG PGDG_KEY_SHA256
# Install ONLY the pinned client, verifying the base distro, the official key hash and the exact
# package version; assert the binaries are major 18, dynamically resolvable and that NO server binary
# is present; then drop the download tools + apt lists (keep ca-certificates for runtime HTTPS).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    . /etc/os-release; \
    test "$VERSION_CODENAME" = "trixie"; \
    ARCH="$(dpkg --print-architecture)"; test "$ARCH" = "amd64"; \
    install -d -m 0755 /usr/share/keyrings; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/keyrings/pgdg.asc; \
    echo "${PGDG_KEY_SHA256}  /usr/share/keyrings/pgdg.asc" | sha256sum -c -; \
    printf 'Types: deb\nURIs: https://apt.postgresql.org/pub/repos/apt\nSuites: %s-pgdg\nComponents: main\nSigned-By: /usr/share/keyrings/pgdg.asc\nArchitectures: %s\n' "$VERSION_CODENAME" "$ARCH" > /etc/apt/sources.list.d/pgdg.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends "postgresql-client-18=${POSTGRESQL_CLIENT_18_VERSION}"; \
    dpkg-query -W -f='${Version}' postgresql-client-18 | grep -qx "${POSTGRESQL_CLIENT_18_VERSION}"; \
    test -x /usr/lib/postgresql/18/bin/pg_restore; \
    test -x /usr/lib/postgresql/18/bin/pg_dump; \
    /usr/lib/postgresql/18/bin/pg_restore --version | grep -Eq 'pg_restore \(PostgreSQL\) 18\.'; \
    /usr/lib/postgresql/18/bin/pg_dump --version | grep -Eq 'pg_dump \(PostgreSQL\) 18\.'; \
    ! ldd /usr/lib/postgresql/18/bin/pg_restore | grep -q 'not found'; \
    ! ldd /usr/lib/postgresql/18/bin/pg_dump | grep -q 'not found'; \
    test ! -e /usr/lib/postgresql/18/bin/postgres; \
    test ! -e /usr/lib/postgresql/18/bin/pg_ctl; \
    test ! -e /usr/lib/postgresql/18/bin/postmaster; \
    test ! -d /var/lib/postgresql/18/main; \
    apt-get purge -y curl gnupg; \
    apt-get autoremove -y --purge; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/*
ENV PATH="/usr/lib/postgresql/18/bin:${PATH}"

# ---------- Builder: resolve and install dependencies with uv ----------
FROM pg-client-base AS builder

# uv provides fast, reproducible, lockfile-driven installs — pinned to a version + digest.
COPY --from=ghcr.io/astral-sh/uv:0.9.8@sha256:08f409e1d53e77dfb5b65c788491f8ca70fe1d2d459f41c89afa2fcbef998abe /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1) Dependency layer only (cached until the lockfile or manifest changes).
#    `readme` in pyproject points at README.md, so it must be present for uv.
COPY apps/api/pyproject.toml apps/api/uv.lock apps/api/README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# 2) Project layer: source, migrations, Alembic config, Dockerfile + trust-root (hashed by every
#    provenance scope, so /app is base-relative-identical to the repo apps/api).
COPY apps/api/src ./src
COPY apps/api/migrations ./migrations
COPY apps/api/alembic.ini ./alembic.ini
COPY apps/api/Dockerfile ./Dockerfile
COPY apps/api/authorization-trust-root.json ./authorization-trust-root.json
RUN uv sync --frozen --no-dev

# 3) Immutable build provenance: a canonical, reproducible document over the runtime bundle.
#    The commit is resolved from BUILD_COMMIT_SHA > RAILWAY_GIT_COMMIT_SHA > APP_COMMIT_SHA; every
#    present value must be 40-hex and two different present values FAIL the build. `railway up` has
#    no `--build-arg` flag but Railway exposes SERVICE variables to the build ONLY when the Dockerfile
#    declares the matching ARG — hence the three ARGs below. The build fails closed without a valid
#    commit; the document is baked into the image and no runtime var can replace it.
#    §4v4: the image bakes ONLY /app/build-provenance.json — NOT a build-provenance.json.sha256
#    sidecar. The separate hash is (re)computed and published solely in CI, never inside the image.
ARG BUILD_COMMIT_SHA=""
ARG RAILWAY_GIT_COMMIT_SHA=""
ARG APP_COMMIT_SHA=""
RUN BUILD_COMMIT_SHA="$BUILD_COMMIT_SHA" \
    RAILWAY_GIT_COMMIT_SHA="$RAILWAY_GIT_COMMIT_SHA" \
    APP_COMMIT_SHA="$APP_COMMIT_SHA" \
    /app/.venv/bin/python -m cestaplan_api.provenance.generate \
        --base /app --out /app/build-provenance.json

# ---------- Runtime: slim, non-root, IMMUTABLE /app (with pinned pg 18 client) ----------
FROM pg-client-base AS runtime

# Unprivileged user with its OWN writable home (NOT /app); any runtime scratch goes to /tmp or $HOME.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Copy the fully-built app (including the .venv) as root — the app user must NOT own /app (§1v3).
COPY --from=builder /app /app

# Lock /app down: root owns everything, and the write bit is removed for EVERYONE. The app user can
# read + execute but cannot modify or (because no directory under /app is writable) create, delete or
# rename any file — including build-provenance.json, the trust-root, code, migrations and .venv.
RUN chown -R root:root /app && chmod -R a-w /app

# PATH puts the pinned pg 18 client bin FIRST, then the app venv; BackupEvidence still resolves
# pg_restore by its FIXED absolute path and verifies it, never relying on PATH selection.
ENV PATH="/usr/lib/postgresql/18/bin:/app/.venv/bin:$PATH" \
    HOME=/home/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    BUILD_PROVENANCE_PATH=/app/build-provenance.json \
    BUILD_AUTHORIZATION_TRUST_ROOT_PATH=/app/authorization-trust-root.json

USER app

EXPOSE 8000

# Default: serve the API. Railway/compose inject $PORT and may override the command
# (worker: `python -m cestaplan_worker.main`; migrations: `alembic upgrade head`).
CMD ["sh", "-c", "uvicorn cestaplan_api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
