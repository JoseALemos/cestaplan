"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.db import engine
from cestaplan_api.logging_config import setup_logging
from cestaplan_api.routers import (
    admin,
    admin_mappings,
    catalog,
    grocery,
    households,
    ingestion_admin,
    invitations,
    licensed_admin,
    pantry,
    plans,
    prices,
    provider_promotion_admin,
    usage,
)
from cestaplan_api.routers import auth as auth_router

settings = get_settings()
# Logging estructurado (JSON a stdout) desde el arranque: baseline de observabilidad self-host.
setup_logging(settings.log_level)
# Falla rápido si se arranca en cloud/producción con seguridad insegura (secreto de sesión por
# defecto o cookies sin Secure). No afecta a dev/self_hosted ni al entorno de tests (que corren
# en self_hosted). Ver Settings.validate_runtime_security.
settings.validate_runtime_security()

_request_log = logging.getLogger("cestaplan.request")


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    """Registra cada petición (método/ruta/estado/duración/request_id) y captura las excepciones
    no controladas con traceback estructurado, SIN filtrar detalle al cliente (Starlette devuelve
    un 500 genérico). Propaga/crea ``X-Request-ID`` para correlacionar. Omite ``/health`` para no
    inundar los logs con los healthchecks periódicos.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            _request_log.exception(
                "unhandled exception",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise
        response.headers["X-Request-ID"] = request_id
        if request.url.path != "/health":
            _request_log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
        return response

app = FastAPI(
    title="CestaPlan API",
    version="0.0.0",
    summary="Planes de alimentación por tienda, presupuesto y preferencias.",
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Añade cabeceras de seguridad a todas las respuestas de la API.

    La API sólo sirve JSON (nunca HTML propio), así que la CSP es máximamente restrictiva:
    no se permite cargar ni enmarcar ningún recurso. HSTS fuerza HTTPS en navegadores.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        return response


def configure_middleware(app: FastAPI, settings: Settings) -> None:
    """Instala las capas: TrustedHost (allowlist) -> CORS -> cabeceras de seguridad.

    El orden de ``add_middleware`` es inverso al de ejecución (la última añadida es la más
    externa): TrustedHost va la última para rechazar Hosts no permitidos antes que nada.
    """
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts_list
    )
    # Observabilidad la más externa (envuelve y cronometra todo, captura excepciones de dentro).
    app.add_middleware(RequestObservabilityMiddleware)


configure_middleware(app, settings)

app.include_router(auth_router.router)
app.include_router(households.router)
app.include_router(invitations.router)
app.include_router(plans.router)
app.include_router(grocery.router)
app.include_router(catalog.router)
app.include_router(admin.router)
app.include_router(admin_mappings.router)
app.include_router(usage.router)
app.include_router(pantry.router)
app.include_router(prices.router)
app.include_router(ingestion_admin.router)
app.include_router(provider_promotion_admin.router)
app.include_router(licensed_admin.router)


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
