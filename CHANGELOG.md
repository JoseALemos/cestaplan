# Changelog

Todos los cambios notables de CestaPlan se documentan en este fichero.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el
proyecto se adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## [Unreleased]

## [0.2.0] — 2026-07-23

**Subsistema de precios (ingesta).** Framework responsable de ingesta de precios,
**desacoplado** del motor de comidas, para descubrir, capturar, parsear, normalizar,
validar y versionar precios **desde fuentes legales y públicas**, sin inventar datos y
**sin scraping** de fuentes bloqueadas. Vive en
`apps/api/src/cestaplan_api/ingestion/` (+ `jobs/`). Viene **apagado por defecto**.

### Added

#### Framework de conectores y pipeline

- Contrato `RetailerConnector` (clase abstracta, sin ORM) con `capabilities()` y
  `source_policy()` honestos y **no-ops seguros** por defecto (degradación elegante).
- `HttpFetcher` resiliente como **único punto de red**: backoff exponencial + jitter,
  conditional-GET (ETag/If-Modified-Since + hash de cuerpo), **circuit breaker** por
  dominio, **guardia SSRF**, lista blanca de dominios, tope de tamaño de respuesta y
  **detección de bloqueo/CAPTCHA que nunca resuelve**.
- Pipeline: captura `RawCapture` (con retención), normalización (€/kg·€/l·€/ud,
  promociones como modelo con fechas de validez), validación, **detección de anomalías**
  (→ cuarentena, **nunca reemplaza el último-bueno**), historial `PriceObservation`
  **append-only**, `CurrentPriceService` (`fresh`/`stale`/`expired`) y
  `CoverageSnapshot` honestos (`partial` nunca como `complete`).
- Cola `CrawlJob` en **Postgres** (`SELECT … FOR UPDATE SKIP LOCKED`, heartbeat,
  backoff, `dead_letter`), **scheduler** idempotente (advisory lock + freshness por
  `(retailer, store, run_type)`) y **worker** con aislamiento por job.

#### Conectores (6; 3 operan, 3 de ofertas desactivados)

- `demofixturemart` (demo sintético, sin red, siempre registrado), `open_prices`
  (real, **ODbL**, con atribución; única fuente de precios reales activa, escasa) y
  `csv_feed` (feed de operador CSV/JSON, `authorized`) — los tres que **operan**.
- `lidl_offers` / `aldi_offers` (ofertas, parciales) y `deza` (regional, vía import)
  **implementados pero desactivados**, `permission_required`/`unsupported`; nunca rastrean.
- Los conectores de catálogo de **Mercadona / Carrefour / Lidl** quedan
  `permission_required` (su `robots.txt` prohíbe sus endpoints de datos): framework
  presente, **nunca ejecutado**.

#### APIs y operación

- **Admin** (`/api/v1/admin/*`, admin + CSRF): `connectors` (enable/disable/health-check),
  `crawls` (cancel/retry), `anomalies` (approve/reject), `coverage`, `sources`,
  `prices/manual`.
- **Consumo** (`/api/v1/*`): `stores/{id}/coverage`, `stores/{id}/catalog-status`,
  `products/search`, `products/{id}/prices`, `prices/current` y
  `POST prices/resolve-basket` (con `unresolved` honesto, nunca fabricado).
- Comandos `python -m cestaplan_api.jobs.{schedule_daily_price_sync, crawl_worker,
  sync_retailer, sync_store, retry_failed, reprocess_capture, connector_health}`; dos
  servicios Railway (`ingestion-scheduler` cron + `ingestion-worker` demonio).
- Documentación nueva: `PRICE_INGESTION`, `CONNECTOR_ARCHITECTURE`,
  `RETAILER_SOURCE_MATRIX`, `SCRAPING_POLICY`, `DATA_RETENTION`, `PRICE_QUALITY`,
  `RAILWAY_PRICE_SYNC`, `INCIDENT_RESPONSE`, `FASE_F_DEPLOYMENT`, `PRICE_SUBSYSTEM_AUDIT`
  y ADR `0008-price-ingestion-subsystem`.
- **~547 tests de backend** (Pytest), con los 11 escenarios de fallo del subsistema
  verificados.

### Security

- **Sin scraping de fuentes bloqueadas**: bloqueos, CAPTCHA, muros de login, anti-bot y
  `robots.txt` se **detectan y reportan, nunca se eluden**; la fuente se detiene y se
  marca (`permission_required` / `temporarily_blocked`).
- **Gating `permission_required`**: habilitar por API un conector `permission_required`/
  `unsupported` devuelve **409 por diseño**; el flag opt-in no anula la política legal.
- **Redacción de secretos**: cabeceras `Authorization`/`Cookie`/`Set-Cookie` y tokens se
  redactan antes de persistir, devolver o loggear; las `RawCapture` se guardan sin
  secretos y con retención acotada (`RAW_CAPTURE_RETENTION_DAYS`).
- Todos los interruptores (`SCRAPING_ENABLED`, `PRICE_SYNC_ENABLED`,
  `*_CONNECTOR_ENABLED`) **desactivados por defecto**.

## [0.1.0] — 2026-07-22

Primera versión abierta (MVP) de CestaPlan: PWA de planes de comida por cadena y
presupuesto, con motor determinista y datos honestos. Prosa en español; identificadores
en inglés.

### Added

#### Núcleo — motor determinista y generación

- Motor determinista que **filtra restricciones duras** (alérgenos, dieta,
  equipamiento), aplica **variedad** (anti-repetición superlineal) y produce planes
  **reproducibles por semilla**.
- **Presupuesto como restricción dura** con **optimización en dos fases**
  (holgado → prioriza variedad, ajustado → ajusta para que quepa, imposible →
  `InfeasibleResult` honesto con el conflicto mínimo de restricciones).
- **Coste por envases completos** en `Decimal` (p. ej. 600 g → 2 × 500 g) con
  **cobertura de precios** explícita (coste conocido vs. estimado, rango si faltan
  datos) y objetivos nutricionales por plan.
- **Generación asíncrona**: `POST` responde `202` con `optimization_run_id`; cola en
  **Postgres** (`SELECT FOR UPDATE SKIP LOCKED`, sin Redis), **worker** dedicado y
  **polling** de estado desde el frontend.
- **OpenAI opcional** (Responses API + JSON Schema): **propone** candidatos que el
  motor **valida**; con IA desactivada o no disponible, **recae en recetas semilla**.
  El contexto enviado a OpenAI se **pseudonimiza**.

#### Fuentes de datos e importación

- Catálogo **demo** sintético completo (`MercaEjemplo`, `is_synthetic=true`): la demo
  del planificador que funciona de extremo a extremo.
- **Importación CSV / JSON / manual** de catálogos propios, con `canonical_name`
  opcional para mapeo 1:1 de ingredientes; trazabilidad vía `DataImport`.
- Enriquecimiento con **Open Food Facts** (nutrición, alérgenos, categorías, imágenes;
  **nunca precios**), bajo **ODbL**.
- **Open Prices** (ODbL): precios reales con **sincronización automática diaria**
  (comando + cron; on-demand por admin) y visor **`/precios`**.
- **Feed comercial** (`authorized_partner`): conector genérico, **opt-in** y
  config-driven que consume una API de pago licenciada por el operador; **sin
  scraping**.
- **Esqueletos de adaptador** de cadena (Aldi, Lidl, Carrefour, Dia, Alcampo, Deza)
  que fijan el contrato sin proporcionar precios por sí solos.
- Reglas de precios inviolables: **nunca inventar precios**, ausencia ≠ 0, no mezclar
  tiendas sin avisar, no usar datos caducados como actuales, **precios por cadena**.

#### Producto y pantallas (PWA)

- **Onboarding** guiado (cadena → miembros → alergias/dietas → preferencias →
  equipamiento → presupuesto con **conmutador variedad-vs-precio** → comidas).
- **Vista de plan** con **panel de objetivos nutricionales** y cobertura de precios;
  **detalle de receta** (ingredientes, pasos, alérgenos, sustituciones, sobras);
  **regenerar** una comida concreta; **favoritos/rechazados**.
- **Lista de la compra** por categorías y envases completos, con exportación
  **CSV/JSON/impresión** y **funcionamiento offline** (PWA/IndexedDB).
- **Despensa** para no comprar de más.
- **Hogar compartido**: invitaciones y roles **owner / editor / viewer**.
- **Admin** para importaciones y sincronización de fuentes.

#### Calidad, infraestructura y despliegue

- Monorepo **pnpm + Turborepo** (JS) y **uv + Python 3.12** (backend); paquetes
  backend `cestaplan_api`, `cestaplan_engine`, `cestaplan_worker`.
- Stack: **Next.js 16 + React 19 + Tailwind v4 + TypeScript estricto + TanStack Query
  + Zod**; **FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic + PostgreSQL**.
- Contratos como fuente única (`packages/contracts`): JSON Schema desde Pydantic v2 →
  tipos TS + esquemas Zod.
- **290 tests de backend** (Pytest); Ruff + Pyright; ESLint + Prettier + Vitest +
  Playwright.
- **CI** en GitHub Actions (`.github/workflows/ci.yml`) y configuraciones de despliegue
  **Railway** en `infra/railway/` (`api`, `web`, `worker`, `open-prices-sync`).
- Modos **`self_hosted` / `cloud`** y facturación de IA **`platform` / `byok` /
  `disabled`**; `UsageLedger` + cuotas en modo cloud.
- Documentación completa en `docs/` y ficheros de comunidad (`LICENSE` MIT,
  `CONTRIBUTING`, `CODE_OF_CONDUCT`, `SECURITY`).

### Security

- Sesión opaca en base de datos con cookie `HttpOnly` / `Secure` / `SameSite`;
  contraseñas con Argon2id; CSRF en endpoints de admin.
- A OpenAI **nunca** se envían nombres reales, email ni identificadores internos.

[Unreleased]: https://example.com/OWNER/cestaplan/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/OWNER/cestaplan/compare/v0.1.0...v0.2.0
[0.1.0]: https://example.com/OWNER/cestaplan/releases/tag/v0.1.0
