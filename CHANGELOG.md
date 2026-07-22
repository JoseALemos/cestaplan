# Changelog

Todos los cambios notables de CestaPlan se documentan en este fichero.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el
proyecto se adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## [Unreleased]

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

[Unreleased]: https://example.com/OWNER/cestaplan/compare/v0.1.0...HEAD
[0.1.0]: https://example.com/OWNER/cestaplan/releases/tag/v0.1.0
