# CestaPlan — developer entrypoints.
# Backend uses uv (Python 3.12); frontend uses pnpm + Turborepo.
# Docker is optional; native targets work without it.

.DEFAULT_GOAL := help
.PHONY: help setup dev up down api web worker migrate seed lint typecheck test fmt

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install all dependencies (Python via uv, JS via pnpm)
	uv sync --project apps/api
	pnpm install

up: ## Start local Postgres + services via docker compose
	docker compose up -d

down: ## Stop docker compose services
	docker compose down

dev: ## Run web + api + worker locally (native, no docker)
	@echo "Run in separate terminals: make api / make web / make worker"

api: ## Run FastAPI (dev)
	uv run --project apps/api uvicorn cestaplan_api.main:app --reload --port $${API_PORT:-8000}

web: ## Run Next.js (dev)
	pnpm --filter @cestaplan/web dev

worker: ## Run the queue worker (dev)
	uv run --project apps/api python -m cestaplan_worker.main

migrate: ## Apply Alembic migrations
	uv run --project apps/api alembic upgrade head

seed: ## Load demo retailer + recipe seed data
	uv run --project apps/api python -m cestaplan_api.scripts.seed_demo

lint: ## Lint everything
	uv run --project apps/api ruff check .
	pnpm lint

typecheck: ## Typecheck everything
	uv run --project apps/api pyright
	pnpm typecheck

test: ## Run all tests
	uv run --project apps/api pytest
	pnpm test

fmt: ## Format code
	uv run --project apps/api ruff format .
	pnpm prettier --write .
