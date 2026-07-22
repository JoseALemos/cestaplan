"""FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from cestaplan_api.config import get_settings
from cestaplan_api.db import engine
from cestaplan_api.routers import admin, catalog, grocery, households, pantry, plans, usage
from cestaplan_api.routers import auth as auth_router

settings = get_settings()

app = FastAPI(
    title="CestaPlan API",
    version="0.0.0",
    summary="Planes de alimentación por tienda, presupuesto y preferencias.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(households.router)
app.include_router(plans.router)
app.include_router(grocery.router)
app.include_router(catalog.router)
app.include_router(admin.router)
app.include_router(usage.router)
app.include_router(pantry.router)


@app.get("/health", tags=["system"])
def health() -> dict[str, object]:
    """Liveness + DB connectivity check (used as Railway healthcheck)."""
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "deployment_mode": settings.deployment_mode,
        "ai_enabled": settings.ai_enabled,
    }
