# CestaPlan backend image (FastAPI api + queue worker + Alembic).
#
# Build context = repository ROOT (matches docker-compose.yml and infra/railway/*.json):
#   docker build -f apps/api/Dockerfile -t cestaplan-api .
#
# One image, three entrypoints (override the command per service):
#   api     -> uvicorn cestaplan_api.main:app --host 0.0.0.0 --port $PORT   (default CMD)
#   worker  -> python -m cestaplan_worker.main
#   migrate -> alembic upgrade head   (Railway pre-deploy command / compose)

# ---------- Builder: resolve and install dependencies with uv ----------
FROM python:3.12-slim AS builder

# uv provides fast, reproducible, lockfile-driven installs.
# Pin to a specific uv release in production, e.g. ghcr.io/astral-sh/uv:0.5.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1) Dependency layer only (cached until the lockfile or manifest changes).
#    `readme` in pyproject points at README.md, so it must be present for uv.
COPY apps/api/pyproject.toml apps/api/uv.lock apps/api/README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# 2) Project layer: source, migrations and Alembic config.
COPY apps/api/src ./src
COPY apps/api/migrations ./migrations
COPY apps/api/alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev

# ---------- Runtime: slim, non-root ----------
FROM python:3.12-slim AS runtime

# Create an unprivileged user to run the app.
RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app

# Copy the fully-built app (including the .venv) from the builder.
COPY --from=builder --chown=app:app /app /app

# Put the virtualenv first on PATH so `uvicorn`, `python` and `alembic` resolve to it.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER app

EXPOSE 8000

# Default: serve the API. Railway/compose inject $PORT and may override the command
# (worker: `python -m cestaplan_worker.main`; migrations: `alembic upgrade head`).
CMD ["sh", "-c", "uvicorn cestaplan_api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
