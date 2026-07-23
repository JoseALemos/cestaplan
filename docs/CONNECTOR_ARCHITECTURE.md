# Arquitectura de conectores

Diseño del contrato `RetailerConnector`, sus capacidades y política de fuente, los
estados de conector, cómo añadir uno nuevo, las garantías del `HttpFetcher` y el
diseño de la cola / scheduler / worker.

Ver también: [`PRICE_INGESTION.md`](PRICE_INGESTION.md) (visión general),
[`SCRAPING_POLICY.md`](SCRAPING_POLICY.md) (reglas de acceso),
[`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md) (estado por supermercado).

---

## 1. El contrato `RetailerConnector`

Definido en `apps/api/src/cestaplan_api/ingestion/contracts.py`. Es una clase
abstracta (`ABC`) que **no importa nada de la capa ORM**, para poder reutilizarse en
workers, adaptadores y tests sin arrastrar SQLAlchemy.

Sólo son abstractos `capabilities()` y `source_policy()` (más las propiedades de
identidad). **Todo método de acceso a datos tiene una implementación por defecto que
devuelve un resultado controlado "no soportado" y nunca lanza excepción.** Así un
conector mínimo declara un conjunto estrecho de capacidades y hereda no-ops seguros
para el resto (**degradación elegante**).

### Identidad

| Atributo | Ejemplo | Uso |
|----------|---------|-----|
| `retailer_code` | `"demofixturemart"` | Código corto y estable del retailer. |
| `connector_version` | `"1.0.0"` | Versión de la lógica de fetch/orquestación. |
| `parser_version` | `"1.0.0"` | Versión de la lógica de parseo (se sube cuando cambia el output). |

### Métodos

| Grupo | Métodos | Por defecto |
|-------|---------|-------------|
| Requeridos | `capabilities()`, `source_policy()` | *abstractos* |
| Salud | `health_check()` | `HealthResult.unsupported()` |
| Descubrimiento | `resolve_store()`, `discover_stores()`, `discover_products(cursor)` | resultado `unsupported` |
| Fetch | `fetch_product()`, `fetch_category()`, `fetch_offers()` | `FetchResult.unsupported()` |
| Parse / normalize | `parse_product()`, `normalize_product()`, `validate_observation()` | `unsupported` |
| Paginación / sync | `get_next_cursor()`, `supports_incremental_sync()`, `supports_conditional_requests()` | `None` / `False` |

Los resultados son *value objects* inmutables (`@dataclass(frozen=True, slots=True)`):
`FetchResult`, `ParseResult`, `ValidationResult`, `HealthResult`,
`StoreResolutionResult`, `NormalizedObservation`, `PromotionInfo`, `SourceRef`. Todos
distinguen `ok` de `supported`, de modo que "no lo soporto" nunca se confunde con "lo
intenté y falló".

---

## 2. `Capabilities` — qué sabe extraer un conector

`@dataclass(frozen=True)` con **todos los flags a `False` por defecto**; un conector
opta sólo a lo que soporta de verdad. Los consumidores los usan para decidir
planificación, expectativa de cobertura y qué etapas del pipeline correr.

| Flag | Significado |
|------|-------------|
| `full_catalog` / `partial_catalog` | Catálogo completo o sólo parcial. |
| `prices` / `promotions` / `loyalty_prices` | Qué precios expone. |
| `availability` | Disponibilidad/stock. |
| `exact_store_scope` / `delivery_zone_scope` / `regional_scope` / `national_scope` | Ámbito geográfico soportado. |
| `product_images` / `barcodes` / `nutrition` | Metadatos de producto. |
| `incremental_sync` | Puede traer sólo lo que cambió. |

Ser honesto aquí es parte del contrato: el `DemoFixtureConnector`, por ejemplo,
declara explícitamente `loyalty_prices=False`, `barcodes=False`, etc., para no
prometer lo que las fixtures no modelan.

---

## 3. `SourcePolicy` — cómo se accede a la fuente

`@dataclass(frozen=True)` que el conector **debe honrar** al hablar con su fuente.

| Campo | Defecto | Significado |
|-------|:-------:|-------------|
| `allowed_domains` | `()` | Lista blanca de dominios; el `HttpFetcher` sólo habla con estos. |
| `request_delay` | `1.0` | Segundos entre peticiones (cota inferior, combinada con el jitter global). |
| `max_concurrency` | `1` | Peticiones en vuelo permitidas (acotado por el techo global). |
| `respects_robots` | `True` | El conector respeta `robots.txt`. |
| `legal_status` | `LegalStatus.UNKNOWN` | Base legal de la fuente. |
| `contact` | `None` | Contacto de operador/abuso. |

### `LegalStatus` (base legal)

`unknown` · `public` · `authorized` · `permission_required` · `prohibited`.

`permission_required` es el estado clave del proyecto: la fuente **prohíbe** el
acceso a sus endpoints de datos (p. ej. por `robots.txt`), así que el conector existe
pero **no se ejecuta** hasta obtener permiso explícito.

---

## 4. Estados de conector — `ConnectorStatus`

Salud operativa de un conector para un retailer/tienda (persistida en
`ConnectorState`).

| Estado | Significado |
|--------|-------------|
| `active` | Operativo y sano. |
| `degraded` | Fallos recientes por debajo del umbral del circuito. |
| `disabled` | Apagado por configuración (flag opt-in a `false`). |
| `unsupported` | La fuente no se soporta (sin conector viable). |
| `permission_required` | Fuente bloqueada/con auth: detenido por política. |
| `temporarily_blocked` | Circuito abierto tras fallos consecutivos; con `circuit_open_until`. |
| `parser_broken` | El parser dejó de extraer datos (cambio de HTML/estructura). |
| `source_unavailable` | La fuente no responde. |
| `partial_only` | Sólo cubre parte del catálogo. |

Transiciones automáticas: el `CrawlWorker` pone `active` en éxito, `degraded` en
fallo, y `temporarily_blocked` con enfriado al superar el umbral (ver §7). El
scheduler no programa retailers cuyo conector esté `disabled`/`unsupported` o con el
circuito abierto.

---

## 5. Cómo añadir un conector

1. **Evaluar la fuente primero.** Revisa términos y `robots.txt`. Si prohíbe el
   acceso a datos, el conector se declara `permission_required` y **no se activa**.
   Documenta la evaluación en [`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md)
   y registra la revisión (`SourceAuditService`, `GET /api/v1/admin/sources`).
2. **Subclase `RetailerConnector`.** Fija `retailer_code`, `connector_version`,
   `parser_version`. Implementa `capabilities()` y `source_policy()` con honestidad;
   hereda no-ops seguros para lo que no soportes.
3. **Implementa sólo lo que declaras.** Si `capabilities().prices` es `True`, aporta
   `discover_products` → `fetch_product` → `parse_product` → `normalize_product`.
   Reutiliza los helpers de `ingestion/normalization.py` (unidades, €/kg, promociones)
   y valida con `ObservationValidator` (`ingestion/validation.py`).
4. **Nunca eludas bloqueos.** Todo fetch pasa por el `HttpFetcher` (§6). Si detecta
   una página de bloqueo/CAPTCHA/login, **reporta y para**; no intentes resolverla.
5. **Registra el conector** en `ingestion/connectors/registry.py` mediante
   `register_connector(retailer_code, factory)`, **detrás de su flag opt-in** (p. ej.
   `MERCADONA_CONNECTOR_ENABLED`). Un retailer sin conector registrado cae en un
   no-op seguro.
6. **Tests deterministas.** Sigue el patrón del `DemoFixtureConnector`: fixtures
   sintéticas, `HttpFetcher` con `httpx.MockTransport`, reloj/`rng`/resolver
   inyectables.

El `DemoFixtureConnector` (`ingestion/connectors/demo.py`) es la referencia
canónica: implementa el contrato completo contra fixtures sintéticas de un retailer
ficticio (`DemoFixtureMart`), sin red, con escenarios deterministas (`baseline`,
`price_change`, `anomaly`, `catalog_drop`, `block_page`).

---

## 6. Garantías del `HttpFetcher`

`apps/api/src/cestaplan_api/ingestion/http_fetcher.py` es el **único punto de red**
por el que fetchan todos los conectores. Es deliberadamente conservador y defensivo.
Nunca lanza por un problema de red/fuente: cada modo de fallo se reporta en el
`HttpFetchResult` (`error` / `is_block_page` / `circuit_open`).

| Garantía | Cómo |
|----------|------|
| **Cortesía** | Timeout acotado, reintentos limitados con backoff exponencial + jitter, concurrencia máxima por dominio y retardo (con jitter) entre peticiones al mismo dominio, User-Agent honesto (+ `From` opcional). |
| **Detección de cambios** | Conditional GET (`If-None-Match` / `If-Modified-Since` a partir de una captura previa) con 304 tratado como *sin cambios*; hash sha256 del cuerpo detecta no-cambio aunque la fuente omita validadores. |
| **Circuit breaker** | Tras `CONNECTOR_FAILURE_THRESHOLD` fallos consecutivos por dominio el circuito se abre `CONNECTOR_CIRCUIT_OPEN_MINUTES`; los fetches siguientes cortocircuitan en vez de martillear la fuente. |
| **Seguridad** | Tope de tamaño de respuesta (aborta descargas grandes), validación MIME, **lista blanca de dominios** y **guardia SSRF** (rechaza IPs privadas/loopback/link-local y esquemas no http(s); las redirecciones cross-host se devuelven, nunca se auto-siguen). |
| **Sin fuga de secretos** | Cabeceras `Authorization` / `Cookie` / `Set-Cookie` y cualquier cabecera con pinta de token se **redactan** antes de almacenarse o devolverse. |
| **Detección de bloqueo (sólo reporta)** | `detect_block_page()` marca 403/429, marcadores de challenge (`captcha`, `just a moment`, `cf-chl`, `access denied`, …) o cuerpos diminutos con palabras de login. **Nunca intenta resolver ni eludir nada.** |
| **Cancelación** | Un callback `cancel` se sondea para abortar pronto. |

El `HttpFetcher` **no decide qué fetchar ni si una fuente puede rastrearse**: eso es
la `SourcePolicy` del conector y la configuración opt-in del operador. Reloj, sleep,
`rng` y resolver DNS son inyectables para tests deterministas.

---

## 7. Cola, scheduler y worker

### 7.1 Cola `CrawlJob` (`ingestion/queue.py`)

Capa fina sobre el modelo `CrawlJob`, con `SELECT ... FOR UPDATE SKIP LOCKED` para
que dos workers nunca tomen la misma fila. El llamante posee la transacción (los
helpers sólo `flush`).

| Capacidad | Detalle |
|-----------|---------|
| **Claim atómico** | `claim_job()` selecciona el siguiente job `queued` con `available_at` vencido, ordenado por prioridad y antigüedad, con `FOR UPDATE OF crawl_job SKIP LOCKED`. |
| **Encolado idempotente** | `enqueue_job()` de-duplica por `idempotency_key` (del argumento o del `payload`); un job con esa clave ya existente se devuelve sin duplicar. |
| **Reintento con backoff** | `fail_job()` incrementa `attempts` y re-encola con backoff exponencial + jitter (base 30 s, tope 3600 s) hasta `max_attempts`; agotado ⇒ `dead_letter`. |
| **Recuperación de stuck** | `recover_stuck_jobs()` re-encola jobs `locked` con heartbeat obsoleto (o ausente) **sin consumir intento** — la recuperación no es un fallo del job. |
| **Límites por retailer** | `domain_limits` (retailer → máximo en vuelo) evita que un retailer monopolice a los workers. |
| **Logging con redacción** | Los campos con pinta de secreto se redactan antes de loggear. |

Estados `JobStatus`: `queued` · `locked` · `completed` · `failed` · `dead_letter` ·
`cancelled`.

### 7.2 Scheduler (`ingestion/scheduler.py`)

`CrawlScheduler.schedule_daily()` convierte retailers/tiendas configurados en
`CrawlRun` + `CrawlJob`, **idempotentemente**:

- **Advisory lock** de Postgres (`pg_try_advisory_xact_lock`, clave
  `SCHEDULER_ADVISORY_LOCK_KEY`): dos schedulers no se interbloquean.
- **`FrequencyConfig` por retailer**: cadencia en días por tipo de run
  (`discovery` 7, `catalog` 3, `prices` 1, `offers` 1), no horas de reloj
  hardcodeadas. Un tipo de run dentro de su ventana de frescura se omite.
- **Idempotencia por clave** `"{slug}:{store_id}:{run_type}:{fecha}"`.
- Variantes forzadas `schedule_retailer()` / `schedule_store()` (para
  `sync_retailer` / `sync_store`) ignoran la comprobación de frescura.

### 7.3 Worker (`ingestion/crawl_worker.py`)

`CrawlWorker.run()` hace polling de la cola y procesa un job a la vez.

- **Dispatch pluggable.** Se le pasa un `registry` `(db, job) -> JobOutcome`. El
  registry real (`ingestion/connectors/registry.py`, `build_worker_registry`)
  resuelve el retailer del job, instancia su conector y corre la orquestación
  completa (`run_crawl_job`). Retailer sin conector ⇒ no-op seguro.
- **Aislamiento total.** `process_job()` envuelve el handler en `try`; una excepción
  sólo falla **ese** job (backoff/dead-letter) y marca `degraded`/`temporarily_blocked`
  su `ConnectorState`. El loop y los demás retailers siguen.
- **Circuit breaker por conector.** Umbral 5 fallos consecutivos, enfriado 15 min
  (constantes por defecto del `CrawlWorker`), reflejado en `ConnectorState`.
- **Apagado limpio.** `StopFlag` conmutada por SIGINT/SIGTERM; recupera stuck-jobs al
  arrancar.

---

## 8. Estado del arte de conectores

| Conector | Retailer | Estado | Notas |
|----------|----------|--------|-------|
| `DemoFixtureConnector` | `demofixturemart` | **Activo** | Sintético, sin red; siempre registrado (`DEMO_ALWAYS_ENABLED`). |
| `OpenPricesConnector` | Open Prices (ODbL) | **Implementado (activo)** | Fuente pública real y legal (`open_dataset`). Implementado como `RetailerConnector` (FASE C) en `ingestion/connectors/openprices.py` y registrado; también sincronizable por `python -m cestaplan_api.scripts.sync_open_prices`. |
| Mercadona / Carrefour / Lidl | — | `permission_required` | Framework presente, **desactivado**; sus endpoints de datos están prohibidos por `robots.txt`. Nunca se rastrean. |
| Dia / Alcampo / Deza | — | `partial_only` / `unsupported` | Evaluación por fuente pendiente. Ver la matriz. |

Detalle honesto por supermercado en
[`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md).
