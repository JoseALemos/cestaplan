# Subsistema de ingesta de precios

> Documentación operativa del subsistema de ingesta de precios de CestaPlan
> (§27 de la especificación; la especificación se refiere a la API de consumo de
> precios como *la API de NutriPlan* — en este repositorio es el producto CestaPlan
> y el código vive en `apps/api/src/cestaplan_api/`).

Este documento describe **qué es** el subsistema, **cómo fluyen los datos** por su
pipeline y **cómo se opera** (comandos, worker, scheduler y variables de entorno).
Documentos hermanos:

- [`CONNECTOR_ARCHITECTURE.md`](CONNECTOR_ARCHITECTURE.md) — el contrato `RetailerConnector`, el `HttpFetcher`, la cola y el worker.
- [`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md) — matriz honesta por supermercado (fuente, autorización, estado).
- [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md) — las reglas estrictas de acceso a fuentes.
- [`DATA_RETENTION.md`](DATA_RETENTION.md) — retención de capturas crudas y minimización de datos.
- [`PRICE_QUALITY.md`](PRICE_QUALITY.md) — modelo de ámbito, tipo, frescura, cobertura y anomalías.
- [`RAILWAY_PRICE_SYNC.md`](RAILWAY_PRICE_SYNC.md) — despliegue del scheduler-cron y del worker en Railway.
- [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) — runbook de incidentes de producción.
- ADR [`adr/0008-price-ingestion-subsystem.md`](adr/0008-price-ingestion-subsystem.md) — decisión de arquitectura.

---

## 1. Qué es (y qué no es)

Es un **framework responsable de ingesta de precios**: la infraestructura para
descubrir, capturar, parsear, normalizar, validar y versionar precios de
supermercado **desde fuentes legales y públicas**, con procedencia completa y sin
inventar datos.

**No** es un scraper. El subsistema **no realiza scraping de fuentes bloqueadas ni
elude** CAPTCHA, muros de login, anti-bot ni `robots.txt`. Las fuentes que prohíben
el acceso a sus endpoints de datos quedan **marcadas y detenidas**
(`permission_required`), con el conector presente pero **desactivado**. La política
completa está en [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md).

El código vive en:

| Ruta | Contenido |
|------|-----------|
| `apps/api/src/cestaplan_api/ingestion/` | Pipeline, contratos, `HttpFetcher`, cola, scheduler, worker. |
| `apps/api/src/cestaplan_api/ingestion/connectors/` | Conectores concretos (`demo.py`) y el `registry.py`. |
| `apps/api/src/cestaplan_api/jobs/` | Comandos CLI (`schedule_daily_price_sync`, `crawl_worker`, …). |
| `apps/api/src/cestaplan_api/models/ingestion.py` | Modelo de datos (`CrawlRun`, `CrawlJob`, `RawCapture`, `PriceObservation`, …). |
| `apps/api/src/cestaplan_api/routers/ingestion_admin.py` | API de administración (`/api/v1/admin/...`). |
| `apps/api/src/cestaplan_api/routers/prices.py` | API de consumo de precios (la "API de NutriPlan" de la especificación). |

---

## 2. El pipeline

El flujo de una observación de precio, de la fuente a la API de consumo, atraviesa
estas etapas. La orquestación end-to-end vive en
`ingestion/orchestration.py` (`run_price_sync` / `run_crawl_job`).

```
Source Discovery → Store Resolution → Capture → Parse → Normalize → Validate
      → Anomaly → Quarantine/Accept → Matching → History → Current → Coverage
      → API de consumo (NutriPlan)
```

| # | Etapa | Módulo | Qué hace |
|---|-------|--------|----------|
| 1 | **Source Discovery** | `connector.discover_products()` | El conector enumera las referencias de producto visibles de la fuente. |
| 2 | **Store Resolution** | `connector.resolve_store()` / `StoreResolutionResult` | Resuelve código postal / id de tienda a un ámbito concreto, con `confidence`. Baja confianza ⇒ se omite la tienda (nunca se inventa). |
| 3 | **Capture** | `ingestion/capture.py` (`RawCaptureRepository`) | Persiste un `RawCapture` inmutable de la respuesta, con cabeceras **redactadas** y retención por resultado (ver [`DATA_RETENTION.md`](DATA_RETENTION.md)). |
| 4 | **Parse** | `connector.parse_product()` | Extrae registros estructurados de la captura cruda. Una página de bloqueo no produce observaciones. |
| 5 | **Normalize** | `ingestion/normalization.py` | Unifica a `Decimal`, €/kg (o €/l, €/ud), cuenta paquetes y parsea promociones (2x1, 3x2, 2ª ud., %, packs) **como modelo**, sin colapsarlas a un único precio. |
| 6 | **Validate** | `ingestion/validation.py` (`ObservationValidator`) | Comprueba `amount > 0`, moneda conocida, `variant_ref` presente, coherencia del precio unitario, `observed_at` no futuro, `price_scope` declarado (`exact_store` exige tienda resuelta) y ventana de promoción sana. Bloqueo/login/CAPTCHA/error ⇒ **quarantine**. |
| 7 | **Anomaly** | `ingestion/anomaly.py` (`AnomalyDetector`) | Compara el lote contra el último-bueno: catálogo −90 %, precio ×100, catálogo vacío, todos-el-mismo-precio, cambio de unidad, cambio de moneda, parser-cero, caída de cobertura. |
| 8 | **Quarantine / Accept** | `ingestion/orchestration.py` | Una anomalía severa (≥ `high`) o validación fallida enruta la observación a *quarantine*. **Nunca** reemplaza automáticamente el último-bueno. |
| 9 | **Matching** | `ingestion/orchestration.py` (`_resolve_variant`) | Resuelve/crea `ExternalProduct` → `Product` canónico → `ProductVariant` (idempotente por `(retailer, external_id)`). |
| 10 | **History** | `ingestion/price_history.py` (`record_observation`) | Historial **append-only** de `PriceObservation`: un cambio cierra el intervalo abierto (`valid_until = as_of`) e inserta una fila nueva; sin cambio revalida en sitio; quarantine se guarda como fila cerrada `disputed` ligada a un `PriceAnomaly`. |
| 11 | **Current** | `ingestion/current_price.py` (`CurrentPriceService`) | Lee la última observación válida por variante con su **frescura** (`fresh`/`stale`/`expired`) y proyecta a la tabla `ProductPrice` que consume el motor de planes. |
| 12 | **Coverage** | `ingestion/coverage.py` (`PriceCoverageService`) | Escribe un `CoverageSnapshot` **honesto**: cobertura parcial se reporta como `partial`, nunca como `complete`. |
| 13 | **API de consumo** | `routers/prices.py`, `routers/catalog.py` | Expone precios, cobertura y estado de catálogo a la aplicación. |

El estado del conector y su circuit breaker (`ConnectorState`) se actualizan en cada
job desde `ingestion/crawl_worker.py`.

---

## 3. Cómo se ejecuta

Todo se lanza como módulos Python del paquete `cestaplan_api`. En Railway cada uno
es un servicio (ver [`RAILWAY_PRICE_SYNC.md`](RAILWAY_PRICE_SYNC.md)); en local o
autohospedado se ejecutan con `python -m ...` dentro del entorno de `apps/api`.

### 3.1 Scheduler (una vez al día)

Crea, de forma **idempotente**, los `CrawlRun` + `CrawlJob` del día para cada
retailer activo con conector usable y tienda configurada.

```bash
python -m cestaplan_api.jobs.schedule_daily_price_sync
```

Dos guardas hacen imposible el doble encolado (ver `ingestion/scheduler.py`):

- **Advisory lock de Postgres** (`pg_try_advisory_xact_lock`): dos schedulers no se
  solapan; el segundo sale sin hacer nada.
- **Freshness por `(retailer, store, run_type)`**: cada tipo de run tiene una
  cadencia en días (`discovery` 7, `catalog` 3, `prices` 1, `offers` 1); si ya
  existe un run dentro de su ventana, se omite. Volver a lanzarlo el mismo día crea
  los jobs exactamente una vez.

### 3.2 Worker (demonio de la cola)

Consume la cola `CrawlJob` en Postgres con `SELECT ... FOR UPDATE SKIP LOCKED`,
procesa un job a la vez y registra el resultado.

```bash
python -m cestaplan_api.jobs.crawl_worker
```

Garantías del worker (`ingestion/crawl_worker.py`):

- **Aislamiento por job.** Cada job se procesa en su propio `try`; el fallo de un
  conector **nunca** detiene el resto ni al worker.
- **Heartbeat + recuperación.** Al arrancar re-encola jobs abandonados por un worker
  muerto (heartbeat obsoleto). Timeout de heartbeat por defecto: 5 min.
- **Backoff / dead-letter.** Un job que falla vuelve a `queued` con backoff
  exponencial + jitter hasta `max_attempts` (3 por defecto); agotado, pasa a
  `dead_letter`.
- **Circuit breaker por conector.** Tras varios fallos consecutivos, `ConnectorState`
  pasa a `temporarily_blocked` con `circuit_open_until`; el scheduler deja de
  programarlo hasta que expire.

### 3.3 Comandos operativos

Todos residen en `apps/api/src/cestaplan_api/jobs/`.

| Comando | Efecto |
|---------|--------|
| `python -m cestaplan_api.jobs.schedule_daily_price_sync` | Planificación diaria idempotente (scheduler). |
| `python -m cestaplan_api.jobs.crawl_worker` | Arranca el loop del worker de la cola. |
| `python -m cestaplan_api.jobs.sync_retailer --retailer <slug>` | Fuerza la programación de un retailer **ahora** (ignora freshness). |
| `python -m cestaplan_api.jobs.sync_store --store-id <uuid>` | Fuerza la programación de una tienda concreta (por `public_id`). |
| `python -m cestaplan_api.jobs.retry_failed --run-id <uuid>` | Re-encola los jobs `failed`/`dead_letter` de un `CrawlRun`. |
| `python -m cestaplan_api.jobs.reprocess_capture --capture-id <uuid>` | Encola un re-parseo de una `RawCapture` almacenada. |
| `python -m cestaplan_api.jobs.connector_health` | Reporta el `ConnectorState` por retailer (estado, fallos, circuito). |

---

## 4. Variables de entorno

Definidas en `apps/api/src/cestaplan_api/config.py` (`Settings`) y documentadas en
`.env.example`. **Todo es opt-in y viene desactivado por defecto.**

### 4.1 Acceso a fuentes (`SCRAPING_*`)

| Variable | Defecto | Significado |
|----------|:-------:|-------------|
| `SCRAPING_ENABLED` | `false` | Interruptor maestro del acceso a red de los conectores. |
| `SCRAPING_USER_AGENT` | `CestaPlanBot/0.0 (+https://github.com/; price-ingestion)` | User-Agent honesto e identificable. |
| `SCRAPING_CONTACT_EMAIL` | `""` | Contacto de abuso; si se define, se envía como cabecera `From`. |
| `SCRAPING_MAX_CONCURRENCY` | `2` | Máximo de peticiones en vuelo por dominio (techo global). |
| `SCRAPING_REQUEST_DELAY_MIN_MS` | `500` | Cota inferior del retardo por dominio (con jitter). |
| `SCRAPING_REQUEST_DELAY_MAX_MS` | `1500` | Cota superior del retardo por dominio (con jitter). |
| `SCRAPING_TIMEOUT_SECONDS` | `20` | Timeout por petición. |
| `SCRAPING_MAX_RETRIES` | `3` | Reintentos ante fallo transitorio (backoff + jitter). |
| `SCRAPING_MAX_RESPONSE_MB` | `5` | Tamaño máximo de respuesta; por encima se aborta la descarga. |

### 4.2 Retención y frescura

| Variable | Defecto | Significado |
|----------|:-------:|-------------|
| `RAW_CAPTURE_RETENTION_DAYS` | `30` | Horizonte de `expires_at` de una `RawCapture`. Ver [`DATA_RETENTION.md`](DATA_RETENTION.md). |
| `STALE_PRICE_HOURS` | `24` | Antigüedad a partir de la cual un precio es `stale`. |
| `EXPIRED_PRICE_HOURS` | `48` | Antigüedad a partir de la cual un precio es `expired`. |

### 4.3 Circuit breaker (capa `HttpFetcher`)

| Variable | Defecto | Significado |
|----------|:-------:|-------------|
| `CONNECTOR_FAILURE_THRESHOLD` | `5` | Fallos consecutivos por dominio antes de abrir el circuito. |
| `CONNECTOR_CIRCUIT_OPEN_MINUTES` | `30` | Minutos que el circuito permanece abierto. |

> Nota honesta: existen **dos** capas de circuit breaker. El `HttpFetcher` abre un
> circuito **por dominio** con los valores de arriba. El `CrawlWorker` mantiene,
> además, un breaker **por conector** sobre `ConnectorState` con umbral 5 y enfriado
> de 15 min (constantes por defecto en `ingestion/crawl_worker.py`). Son
> complementarios: uno protege la red, el otro la planificación.

### 4.4 Flags por conector (todos `false`)

Cada conector real es opt-in y viene **desactivado**. Los conectores de fuentes con
acceso prohibido por `robots.txt` (`permission_required`) **no se activan** por estos
flags: siguen detenidos por política (ver [`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md)).

| Variable | Defecto |
|----------|:-------:|
| `MERCADONA_CONNECTOR_ENABLED` | `false` |
| `CARREFOUR_CONNECTOR_ENABLED` | `false` |
| `DIA_CONNECTOR_ENABLED` | `false` |
| `ALCAMPO_CONNECTOR_ENABLED` | `false` |
| `LIDL_OFFERS_CONNECTOR_ENABLED` | `false` |
| `ALDI_OFFERS_CONNECTOR_ENABLED` | `false` |
| `DEZA_CONNECTOR_ENABLED` | `false` |

El conector `DemoFixtureConnector` (retailer `demofixturemart`) es **sintético y sin
red**, por lo que está siempre registrado y no depende de ningún flag.

---

## 5. APIs

### 5.1 Administración — `/api/v1/admin/*` (`routers/ingestion_admin.py`)

| Método y ruta | Uso |
|---------------|-----|
| `GET /connectors`, `GET /connectors/{code}` | Listar conectores y su estado. |
| `POST /connectors/{code}/enable` · `/disable` · `/health-check` | Operar un conector. |
| `GET /crawls`, `GET /crawls/{crawl_id}` | Inspeccionar runs de rastreo. |
| `POST /crawls/{crawl_id}/cancel` · `/retry` | Cancelar / reintentar un run. |
| `GET /anomalies` · `POST /anomalies/{id}/approve` · `/reject` | Revisar cuarentena. |
| `GET /coverage` | Snapshots de cobertura por retailer/tienda. |
| `GET /sources` | Fuentes con su `legal_status` y fechas de revisión de términos/robots. |

### 5.2 Consumo de precios — `/api/v1/*` (`routers/prices.py`, `routers/catalog.py`)

| Método y ruta | Uso |
|---------------|-----|
| `GET /stores/{store_id}/coverage` | Cobertura honesta de una tienda. |
| `GET /stores/{store_id}/catalog-status` | Estado del catálogo de una tienda. |
| `GET /products/search` | Búsqueda de productos. |
| `GET /products/{variant_id}/prices` | Historial de precios de una variante. |
| `GET /prices/current` | Precio actual con frescura. |
| `POST /prices/resolve-basket` | Resolución de una cesta a precios actuales. |
| `GET /retailers/{retailer_id}/stores/{store_id}/prices` | Visor "Precios reales" (Open Prices, ODbL). |

---

## 6. Invariantes que el subsistema garantiza

- **Dinero con `Decimal`**, nunca `float` (ver ADR [`0003`](adr/0003-decimal-money.md)).
- **Historial append-only**: los precios no se reescriben destructivamente.
- **Nunca reemplazar el último-bueno** con un lote sospechoso.
- **Nunca presentar parcial como completo** ni **inventar** un precio, ni poner `0`
  por un dato ausente.
- **Nunca almacenar secretos**: cabeceras y cookies se redactan antes de persistir.
- **Nunca eludir** bloqueos, CAPTCHA o `robots.txt`: se detecta, se reporta y se para.
