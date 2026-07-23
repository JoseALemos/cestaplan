# CestaPlan

**Del presupuesto al plato, sin sorpresas en la caja.**

CestaPlan es una PWA de código abierto (MIT) que genera **planes de comida
personalizados**, conscientes de la **tienda** donde compras y de **cuánto quieres
gastar**. Le dices la cadena, el presupuesto, para cuántas personas y qué comidas
necesitas; un **motor determinista** genera recetas, calcula los **envases completos**
que hay que comprar y prepara una lista de la compra que funciona **offline**.

<!-- Badges placeholder — sustituir por los reales cuando exista CI público/publicación -->
[![CI](https://img.shields.io/badge/CI-pending-lightgrey.svg)](./.github/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)
[![PWA](https://img.shields.io/badge/PWA-mobile--first-blue.svg)](#)

---

## La promesa

> "Dime dónde compras, cuánto quieres gastar, para cuántas personas y qué comidas
> necesitas. CestaPlan genera recetas, calcula los envases necesarios y prepara una
> lista de compra adaptada a una tienda concreta."

El presupuesto no es una estimación optimista: es una **restricción dura**. Los
precios llevan siempre **fuente + tienda + fecha**, y cuando no hay dato, CestaPlan
lo dice en lugar de inventarlo.

## Qué es CestaPlan

- Un **planificador de comidas consciente del presupuesto y de la tienda**.
- Un **motor determinista** que filtra restricciones duras (alérgenos, dieta,
  equipamiento), aplica **variedad** (anti-repetición superlineal), trata el
  **presupuesto como restricción dura** (optimización en dos fases) y calcula el
  coste por **envases completos**, produciendo planes **reproducibles por semilla**
  y auditables.
- Una **PWA mobile-first** (Next.js 16 + React 19) con lista de la compra que
  funciona **offline** (estado local en IndexedDB).
- **Self-hostable**: todas las funciones críticas operan **sin OpenAI**.

## Qué NO es CestaPlan

- **No** es un comparador de precios listo para usar. No trae catálogos comerciales
  cargados; tú aportas los datos (importación CSV/JSON, entrada manual, datasets
  abiertos o un feed comercial que licencies).
- **No** hace *scraping* ni elude CAPTCHA/anti-bot. Respeta `robots.txt` y los
  mecanismos anti-bot de terceros.
- **No** da consejo médico ni nutricional profesional. Es una herramienta de
  planificación con información **orientativa**.
- El **LLM no decide seguridad de alergias ni cálculos económicos**: sólo propone
  candidatos; el núcleo determinista valida y calcula.

## Principios clave

1. **El presupuesto es una restricción real.** Precios con fuente, tienda y fecha.
   Nunca inventar precios.
2. **Las alergias son una restricción DURA.** El validador determinista, no el LLM,
   decide qué es seguro.
3. **OpenAI propone; el núcleo determinista valida y calcula.** Planificación
   reproducible con semilla y auditable.
4. **Toda función crítica funciona sin OpenAI.** La IA es opcional y desactivable.
5. **El dinero es exacto.** `Decimal` en Python / `numeric` en Postgres; en JS el
   dinero viaja como **string**. Nunca `float`.
6. **Envases completos.** Si una receta necesita 600 g de pollo y la bandeja es de
   500 g, se compran **2 bandejas** (1000 g comprados, 400 g de sobrante). No se
   prorratea `600/500 × precio`.
7. **Datos honestos.** No *scraping*; conectores comunitarios desactivados por
   defecto; nunca presentar estimaciones como datos reales.
8. **Privacidad primero.** A OpenAI nunca se envían nombres reales, email ni
   identificadores internos: el contexto se pseudonimiza.

## Funcionalidades

### Motor determinista (núcleo)

- **Filtrado duro** de alérgenos, restricciones dietéticas y equipamiento de cocina.
- **Variedad**: penalización superlineal de la repetición para no proponer el mismo
  plato una y otra vez.
- **Presupuesto como restricción dura** con **optimización en dos fases**: si hay
  holgura, prioriza variedad; si está justo, ajusta para que **quepa**; si es
  imposible, devuelve un **`InfeasibleResult` honesto** con el conflicto mínimo de
  restricciones y cómo relajarlas, en vez de un plan falso.
- **Coste por envases completos** (`Decimal`), con **cobertura de precios** explícita
  (coste conocido vs. estimado, rango cuando faltan datos).
- **Nutrición** y objetivos nutricionales por plan.
- **Reproducible por semilla**: la misma entrada produce el mismo plan.

### IA opcional (OpenAI)

- OpenAI (Responses API + JSON Schema) **propone** candidatos de receta que el motor
  **valida**. Es **opcional**: con la IA desactivada o no disponible, el sistema
  **recae en recetas semilla** y todo lo crítico sigue funcionando.

### Ingesta de precios (opcional, desacoplada)

- **Subsistema responsable de ingesta de precios**: framework de conectores
  (`RetailerConnector`), `HttpFetcher` resiliente, pipeline con historial append-only
  y cuarentena de anomalías, cola/scheduler/worker en Postgres, y APIs de admin y de
  consumo (`resolve-basket`). **No hace scraping** de fuentes bloqueadas y viene
  **apagado por defecto**. Ver [Subsistema de precios (ingesta)](#subsistema-de-precios-ingesta).

### Producto / pantallas

- **Onboarding** guiado: cadena → miembros del hogar → alergias/dietas →
  preferencias → equipamiento → **presupuesto con conmutador variedad-vs-precio** →
  comidas.
- **Generación asíncrona**: `POST` responde `202` con `optimization_run_id`; el
  trabajo entra en una **cola en Postgres**, lo procesa un **worker** y el frontend
  hace **polling** del estado.
- **Vista de plan** con **panel de objetivos nutricionales** y cobertura de precios.
- **Detalle de receta**: ingredientes, pasos, alérgenos, sustituciones, sobras;
  regenerar una comida concreta; marcar **favorito/rechazado**.
- **Lista de la compra**: agrupada por categorías, por envases completos, con
  exportación **CSV/JSON/impresión** y **funcionamiento offline** (PWA/IndexedDB).
- **Despensa**: gestiona lo que ya tienes en casa para no comprarlo de más.
- **Favoritos y rechazados**: influyen en la puntuación de futuros planes.
- **Hogar compartido**: invitaciones y roles **owner / editor / viewer**.
- **Visor `/precios`**: muestra datos **reales** de Open Prices por tienda.
- **Admin**: importaciones (CSV/JSON) y sincronización de fuentes de datos.

## Fuentes de datos

Los **precios son por CADENA** (se agregan todas las tiendas de una misma cadena).
Fuentes soportadas (detalle y licencias en [docs/DATA_SOURCES.md](./docs/DATA_SOURCES.md)):

- **Demo** (`MercaEjemplo`): catálogo sintético completo (`is_synthetic=true`). Es la
  **demo del planificador que funciona de principio a fin**.
- **Importación CSV / JSON / manual**: tu propio catálogo del que dispones
  legítimamente, con `canonical_name` opcional para un **mapeo 1:1** de ingredientes.
- **Open Food Facts** (ODbL): enriquecimiento de **nutrición, alérgenos, categorías e
  imágenes**. **Nunca precios.**
- **Open Prices** (ODbL): **precios reales** de la comunidad de Open Food Facts, con
  **sincronización automática diaria**.
- **Feed comercial** (`authorized_partner`): conector **genérico, opt-in y
  config-driven** que **consume una API de pago** que licencias tú. **No hace
  scraping.**
- **Esqueletos de cadena** (Aldi, Lidl, Carrefour, Dia, Alcampo, Deza): fijan el
  contrato del adaptador; **no** proporcionan precios por sí solos.

## Estado de los datos de precios (importante y honesto)

CestaPlan es honesto sobre una limitación real, y esto es una **característica**, no
un defecto:

> **Los precios de supermercado reales NO están disponibles de forma legal y densa
> hoy.** No hay scraping. La única fuente abierta de precios reales, **Open Prices**,
> es legítima pero **escasa** y cubre sobre todo **los productos equivocados** (snacks,
> aceites…), no los básicos de receta. Por eso, sobre una cadena real, la **cobertura
> de precios es casi nula** a día de hoy y muchos planes quedan como *coste estimado*
> o sin coste conocido.

El **coste completo funciona con un catálogo denso**:

- la **demo** (`MercaEjemplo`), que funciona de extremo a extremo hoy mismo;
- un **catálogo importado** del que tengas derechos (CSV/JSON/manual);
- un **feed comercial licenciado** que conectes.

CestaPlan **prefiere decir "no tengo este precio" antes que inventarlo**. Cuando falta
un precio, la línea entra en "coste estimado" o queda sin coste conocido, nunca en `0`.

## Subsistema de precios (ingesta)

Desacoplado del motor de comidas, CestaPlan incluye un **subsistema responsable de
ingesta de precios**: la infraestructura para descubrir, capturar, parsear, normalizar,
validar y versionar precios de supermercado **desde fuentes legales y públicas**, con
procedencia completa y **sin inventar datos**. Vive en
`apps/api/src/cestaplan_api/ingestion/` (+ `jobs/`) y alimenta las proyecciones
`ProductPrice` que consume el motor; el motor sigue funcionando aunque la ingesta esté
apagada. Documentación operativa completa en
[docs/PRICE_INGESTION.md](./docs/PRICE_INGESTION.md).

**No es un scraper.** El subsistema **no realiza scraping de fuentes bloqueadas ni
elude** CAPTCHA, muros de login, anti-bot ni `robots.txt`: los **detecta y reporta,
nunca los resuelve**. Una fuente que prohíbe el acceso a sus endpoints de datos se
**detiene y se marca** (`permission_required`), con el conector presente pero
**desactivado**. Detalle en [docs/SCRAPING_POLICY.md](./docs/SCRAPING_POLICY.md).

### Framework de conectores y pipeline

- Contrato `RetailerConnector` (clase abstracta, sin ORM) con `capabilities()` y
  `source_policy()` honestos y **no-ops seguros** por defecto (degradación elegante).
- `HttpFetcher` resiliente y **único punto de red**: backoff exponencial + jitter,
  conditional-GET (ETag/If-Modified-Since + hash de cuerpo), **circuit breaker** por
  dominio, **guardia SSRF** (rechaza IPs privadas/loopback y esquemas no http(s)),
  lista blanca de dominios, **detección de bloqueo/CAPTCHA que nunca resuelve** y
  **redacción de secretos** (cabeceras `Authorization`/`Cookie`/tokens) antes de
  persistir o devolver nada.
- Pipeline: `Discovery → Store Resolution → Capture (RawCapture con retención) →
  Parse → Normalize (€/kg·€/l·€/ud, promociones como modelo con fechas de validez) →
  Validate → Anomaly → Quarantine/Accept → Matching → History → Current → Coverage`.
  El **historial `PriceObservation` es append-only**; una anomalía severa o una
  validación fallida va a **cuarentena** y **nunca reemplaza el último-bueno**.
- `CurrentPriceService` expone frescura (`fresh`/`stale`/`expired`) y
  `PriceCoverageService` escribe `CoverageSnapshot` **honestos** (parcial se reporta
  como `partial`, nunca como `complete`).
- Cola `CrawlJob` en **Postgres** (`SELECT … FOR UPDATE SKIP LOCKED`, heartbeat,
  backoff, `dead_letter`), **scheduler** idempotente (advisory lock + freshness por
  `(retailer, store, run_type)`) y **worker** con aislamiento por job. Diseño en
  [docs/CONNECTOR_ARCHITECTURE.md](./docs/CONNECTOR_ARCHITECTURE.md).

### Conectores (estado honesto)

Seis conectores registrados; **tres operan** y **tres son de ofertas, desactivados**.
Ninguna cadena real se rastrea. Matriz completa en
[docs/RETAILER_SOURCE_MATRIX.md](./docs/RETAILER_SOURCE_MATRIX.md).

| `retailer_code` | Tipo | ¿Opera? | Base legal |
|-----------------|------|:-------:|------------|
| `demofixturemart` | Demo sintético, **sin red** | **Sí** (siempre registrado) | Dato sintético |
| `open_prices` | `open_dataset` real (**ODbL**, con atribución) | **Sí** | Pública — Open Prices (Open Food Facts). Única fuente de **precios reales** activa; escasa. |
| `csv_feed` | Feed de operador (CSV/JSON) | **Sí** | `authorized` — feed que el operador **aporta legítimamente** |
| `lidl_offers` / `aldi_offers` | Ofertas (parcial, nunca catálogo completo) | **No** | `permission_required` — desactivados; nunca rastrean |
| `deza` | Regional; ruta real = **import por admin** | **No** | `unsupported` (scraping) / `permission_required` |

Los conectores de **catálogo** de Mercadona, Carrefour y Lidl son
`permission_required`: su `robots.txt` prohíbe sus endpoints de datos
(`/api`, `/supermercado/ajax`, `/user-api`), así que existen como framework pero
**nunca se ejecutan**. Activar su flag opt-in **no** los pone a rastrear; la API de
admin devuelve **409** al intentar habilitar un conector `permission_required`/
`unsupported`.

### APIs

- **Administración** (`/api/v1/admin/*`, admin + CSRF):
  `connectors` (listar / `enable` / `disable` / `health-check`), `crawls`
  (`cancel` / `retry`), `anomalies` (`approve` / `reject`), `coverage`, `sources`
  (footing legal y fechas de revisión de términos/`robots`), `prices/manual` (registrar
  un precio tecleado por el operador, append-only y auditado).
- **Consumo de precios** (`/api/v1/*`): `stores/{id}/coverage`,
  `stores/{id}/catalog-status`, `products/search`, `products/{id}/prices`,
  `prices/current` (con frescura) y `POST prices/resolve-basket` (resuelve una cesta a
  precios actuales, listando honestamente lo **no resuelto**, nunca fabricado).

### Cómo se ejecuta (todo opt-in, apagado por defecto)

Módulos Python del paquete `cestaplan_api`, en Railway como dos servicios sin dominio
público (ver [docs/RAILWAY_PRICE_SYNC.md](./docs/RAILWAY_PRICE_SYNC.md) y
[docs/FASE_F_DEPLOYMENT.md](./docs/FASE_F_DEPLOYMENT.md)):

```bash
python -m cestaplan_api.jobs.schedule_daily_price_sync   # scheduler (cron diario idempotente)
python -m cestaplan_api.jobs.crawl_worker                # worker de la cola (demonio)
python -m cestaplan_api.jobs.sync_retailer  --retailer <slug>   # forzar un retailer ahora
python -m cestaplan_api.jobs.sync_store     --store-id <uuid>   # forzar una tienda
python -m cestaplan_api.jobs.retry_failed   --run-id  <uuid>    # re-encolar dead-letter
python -m cestaplan_api.jobs.reprocess_capture --capture-id <uuid>   # re-parsear una captura
python -m cestaplan_api.jobs.connector_health                   # salud por conector
```

`SCRAPING_ENABLED`, `PRICE_SYNC_ENABLED` y todos los `*_CONNECTOR_ENABLED` vienen a
`false` por defecto: activar un conector es una decisión consciente del operador y
**sólo** para fuentes cuya evaluación (términos + `robots.txt`) lo permita.

### Realidad honesta de los datos

Los **precios reales completos por cadena no son accesibles legalmente** hoy (sin feed
oficial; `robots.txt` de las grandes prohíbe sus endpoints). El coste al 100 % de un
plan sólo se logra con un **catálogo denso**: la **demo**, un **`csv_feed`** que aportes
con derechos, o un **feed comercial licenciado**. **Open Prices** es real y legal pero
**escaso** (cubre sobre todo productos que las recetas no compran), así que sobre una
cadena real la cobertura es casi nula — y se muestra como tal, nunca como completa.
Auditoría de producción y escenarios de fallo verificados en
[docs/PRICE_SUBSYSTEM_AUDIT.md](./docs/PRICE_SUBSYSTEM_AUDIT.md); calidad, frescura y
anomalías en [docs/PRICE_QUALITY.md](./docs/PRICE_QUALITY.md); retención de capturas en
[docs/DATA_RETENTION.md](./docs/DATA_RETENTION.md); runbook de incidentes en
[docs/INCIDENT_RESPONSE.md](./docs/INCIDENT_RESPONSE.md).

## Stack

| Capa | Tecnología |
|------|-----------|
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript **estricto** + Tailwind v4 + TanStack Query + React Hook Form + Zod + PWA (service worker) + IndexedDB |
| Backend | Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic + HTTPX + SDK oficial de OpenAI |
| Base de datos | PostgreSQL (cola de trabajos con `SELECT FOR UPDATE SKIP LOCKED`, sin Redis) |
| Contratos | Fuente única en `packages/contracts`: JSON Schema desde Pydantic v2 → tipos TS + esquemas Zod |
| Calidad | Ruff + Pyright (Python) · ESLint + Prettier + Vitest + Playwright (JS) · Pytest (**~547 tests de backend**, incluida la ingesta de precios) |
| Infra | Docker + docker-compose + GitHub Actions + Railway |
| Monorepo | pnpm workspaces + Turborepo (JS) · uv + Python 3.12 (backend) |

## Estructura del monorepo

```
cestaplan/
├── apps/
│   ├── web/            # PWA Next.js 16 (App Router)
│   ├── api/            # FastAPI + motor determinista + integración OpenAI
│   │                   #   paquetes: cestaplan_api · cestaplan_engine · cestaplan_worker
│   └── worker/         # Worker de la cola de trabajos (Postgres)
├── packages/
│   ├── contracts/      # Fuente única de contratos (JSON Schema → TS + Zod)
│   ├── ui/             # Componentes de interfaz compartidos
│   └── config/         # Configuración compartida (lint, tsconfig, etc.)
├── data/
│   ├── demo/           # Datos demo sintéticos (is_synthetic=true)
│   ├── imports/        # Importaciones de precios/catálogos (CSV/JSON)
│   └── schemas/        # Esquemas de datos e importación
├── docs/               # Documentación (ver más abajo)
├── infra/railway/      # Configuración de despliegue en Railway
├── scripts/            # Utilidades de desarrollo
├── docker-compose.yml
├── Makefile
├── package.json · pnpm-workspace.yaml · turbo.json
├── CHANGELOG.md · LICENSE · .env.example
└── README.md
```

Rutas principales de la web: `/registro`, `/login`, `/onboarding`, `/planes`,
`/recetas`, `/despensa`, `/favoritos`, `/households`, `/invitaciones`, `/precios`,
`/admin`.

## Requisitos

- **Node.js 22+** y **pnpm** (`packageManager: pnpm@10.30.3`).
- **uv** + **Python 3.12** para el backend.
- **PostgreSQL** (local o gestionado).
- **Docker** (opcional): los targets nativos del `Makefile` funcionan sin Docker.
- **OpenAI**: opcional. En modo BYOK aportas tu propia `OPENAI_API_KEY`; con
  `AI_BILLING_MODE=disabled` la IA queda desactivada y todo lo crítico sigue
  funcionando.

## Arranque rápido

Primero, copia el fichero de entorno:

```bash
cp .env.example .env
# edita .env según tu entorno (DATABASE_URL, SESSION_SECRET, OPENAI_* si usas IA)
```

### Opción A — con Docker (todo en contenedores)

Levanta Postgres + api + worker + web replicando el modelo de servicios de Railway:

```bash
make up            # docker compose up -d
# web:  http://localhost:3000
# api:  http://localhost:8000  (health: /health)
```

Para parar:

```bash
make down
```

### Opción B — nativo (uv + pnpm + Makefile)

```bash
make setup         # uv sync (apps/api) + pnpm install

# arranca solo Postgres con docker (o usa tu Postgres local)
make up

make migrate       # alembic upgrade head
make seed          # carga datos demo (MercaEjemplo + recetas sintéticos)

# en terminales separadas:
make api           # FastAPI en :8000
make web           # Next.js en :3000
make worker        # worker de la cola
```

Targets útiles del `Makefile`: `make lint`, `make typecheck`, `make test`,
`make fmt`. Ejecuta `make help` para ver todos.

Con la demo cargada, el planificador funciona **de principio a fin** sobre el catálogo
sintético `MercaEjemplo` (cobertura de precios completa).

## Modos de despliegue y facturación de IA

Configurables por variables de entorno (ver `.env.example`):

- **`DEPLOYMENT_MODE`** = `self_hosted` | `cloud`
  - `self_hosted`: el admin aporta `OPENAI_API_KEY`, sin límites por defecto,
    puede **desactivar la IA** e importar sus propios catálogos.
  - `cloud`: la clave la gestiona el servidor, se registra el consumo
    (`UsageLedger`), se aplican cuotas y **nunca se revela la clave**. Sin pagos
    en el MVP.
- **`AI_BILLING_MODE`** = `platform` | `byok` | `disabled`
  - `platform`: la plataforma factura el uso.
  - `byok`: *bring your own key* — usas tu propia clave de OpenAI.
  - `disabled`: sin IA; el motor determinista lo hace todo.

El modelo **no** se hardcodea en la lógica de negocio: se configura vía
`OPENAI_MODEL`, `OPENAI_REASONING_EFFORT`, `OPENAI_TIMEOUT_SECONDS`,
`OPENAI_MAX_RETRIES`.

## Documentación

| Documento | Contenido |
|-----------|-----------|
| [docs/PRD.md](./docs/PRD.md) | Requisitos de producto |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | Arquitectura del sistema |
| [docs/DATA_MODEL.md](./docs/DATA_MODEL.md) | Modelo de datos y entidades |
| [docs/DATA_SOURCES.md](./docs/DATA_SOURCES.md) | Fuentes de datos y licencias |
| [docs/OPTIMIZATION.md](./docs/OPTIMIZATION.md) | Motor determinista y optimización |
| [docs/OPENAI.md](./docs/OPENAI.md) | Integración con OpenAI (qué puede y qué no) |
| [docs/SECURITY.md](./docs/SECURITY.md) | Seguridad (detalle) |
| [docs/PRIVACY.md](./docs/PRIVACY.md) | Privacidad y datos sensibles |
| [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) | Despliegue (Railway, Docker) |
| [docs/PUBLISHING.md](./docs/PUBLISHING.md) | Checklist y pasos de publicación en GitHub |
| [docs/CONTRIBUTING.md](./docs/CONTRIBUTING.md) | Contribución (remite a la guía raíz) |
| [docs/ADAPTER_GUIDE.md](./docs/ADAPTER_GUIDE.md) | Guía de adaptadores de supermercado |
| [docs/RECIPES_GUIDE.md](./docs/RECIPES_GUIDE.md) | Guía para contribuir recetas |
| [docs/PRICE_SOURCES_GUIDE.md](./docs/PRICE_SOURCES_GUIDE.md) | Guía de fuentes de precios |
| [docs/PRICE_INGESTION.md](./docs/PRICE_INGESTION.md) | Subsistema de ingesta de precios (visión general, pipeline, comandos) |
| [docs/CONNECTOR_ARCHITECTURE.md](./docs/CONNECTOR_ARCHITECTURE.md) | Contrato `RetailerConnector`, `HttpFetcher`, cola/scheduler/worker |
| [docs/RETAILER_SOURCE_MATRIX.md](./docs/RETAILER_SOURCE_MATRIX.md) | Matriz honesta por supermercado (fuente, autorización, estado) |
| [docs/SCRAPING_POLICY.md](./docs/SCRAPING_POLICY.md) | Política estricta de acceso a fuentes (no scraping de bloqueadas) |
| [docs/DATA_RETENTION.md](./docs/DATA_RETENTION.md) | Retención de capturas crudas y minimización de datos |
| [docs/PRICE_QUALITY.md](./docs/PRICE_QUALITY.md) | Ámbito, tipo, frescura, cobertura y anomalías de precios |
| [docs/RAILWAY_PRICE_SYNC.md](./docs/RAILWAY_PRICE_SYNC.md) | Despliegue del scheduler-cron y el worker de ingesta en Railway |
| [docs/INCIDENT_RESPONSE.md](./docs/INCIDENT_RESPONSE.md) | Runbook de incidentes de la ingesta en producción |
| [docs/FASE_F_DEPLOYMENT.md](./docs/FASE_F_DEPLOYMENT.md) | Playbook operativo de despliegue de la ingesta |
| [docs/PRICE_SUBSYSTEM_AUDIT.md](./docs/PRICE_SUBSYSTEM_AUDIT.md) | Auditoría de producción y escenarios de fallo verificados |
| [docs/ROADMAP.md](./docs/ROADMAP.md) | Hoja de ruta por fases |
| [docs/adr/](./docs/adr/) | Architecture Decision Records (incl. `0008-price-ingestion-subsystem`) |

Historial de cambios: [CHANGELOG.md](./CHANGELOG.md).

## Contribuir

Lee la guía de contribución: [CONTRIBUTING.md](./CONTRIBUTING.md). Comunidad y
políticas: [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) ·
[SECURITY.md](./SECURITY.md) · GitHub Discussions. Las *pull requests* son bienvenidas;
busca las incidencias etiquetadas **`good first issue`** para empezar.

## Licencia

El **código fuente** de CestaPlan está bajo licencia [MIT](./LICENSE).

Los **datos** (catálogos de producto, extractos de Open Food Facts, precios de Open
Prices, datos demo, etc.) se rigen por **sus propias licencias**, documentadas con su
procedencia en [docs/DATA_SOURCES.md](./docs/DATA_SOURCES.md). En particular, **Open
Food Facts** y **Open Prices** se distribuyen bajo **ODbL** (requieren **atribución** y
*share-alike* de la base de datos). No asumas que un catálogo comercial se puede
redistribuir bajo la MIT: por defecto es `proprietary` y no sale del despliegue del
operador.

## Aviso sanitario

> CestaPlan facilita la planificación y ofrece información orientativa. No sustituye
> el consejo de un profesional sanitario. Comprueba siempre las etiquetas de los
> productos en caso de alergia o intolerancia.
