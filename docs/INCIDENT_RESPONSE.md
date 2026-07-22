# Runbook de incidentes de ingesta de precios

Mapa de los escenarios de producción a **cómo se comporta el sistema** y **qué debe
hacer el operador**. La filosofía del subsistema es que casi todos estos casos se
manejan solos de forma segura (cuarentena, backoff, circuit breaker, idempotencia);
la intervención humana es para diagnosticar y, cuando toca, corregir la causa raíz.

Ver también: [`PRICE_INGESTION.md`](PRICE_INGESTION.md),
[`PRICE_QUALITY.md`](PRICE_QUALITY.md),
[`CONNECTOR_ARCHITECTURE.md`](CONNECTOR_ARCHITECTURE.md),
[`RAILWAY_PRICE_SYNC.md`](RAILWAY_PRICE_SYNC.md).

---

## 0. Cómo inspeccionar (herramientas comunes)

| Vía | Uso |
|-----|-----|
| `python -m cestaplan_api.jobs.connector_health` | Estado de cada conector (`ConnectorState`): status, fallos consecutivos, `circuit_open_until`, último error. |
| `GET /api/v1/admin/connectors` · `/connectors/{code}` | Igual, vía API de administración. |
| `GET /api/v1/admin/crawls` · `/crawls/{crawl_id}` | Runs de rastreo y su resultado (contadores discovered/accepted/quarantined/errors). |
| `GET /api/v1/admin/anomalies` | Cola de cuarentena; `POST .../approve` · `/reject` para resolver. |
| `GET /api/v1/admin/coverage` | Snapshots de cobertura por retailer/tienda. |
| `GET /api/v1/admin/sources` | Base legal y fechas de revisión de términos/robots por fuente. |
| SQL directo | La cola vive en Postgres: `CrawlJob`, `CrawlRun`, `PriceObservation`, `PriceAnomaly`, `RawCapture`, `ConnectorState`. |

Recuperación genérica: `retry_failed --run-id <uuid>` (re-encola failed/dead-letter),
`reprocess_capture --capture-id <uuid>` (re-parsea una captura), `sync_retailer` /
`sync_store` (fuerza una re-programación tras corregir la causa).

---

## 1. Cambia el HTML / el parser deja de extraer

**Síntoma:** un run trae 0 observaciones o el parser produce basura.

**Comportamiento del sistema:** `parser_returned_zero` (anomalía `high`) y/o el corte
de catálogo vacío; las observaciones no válidas van a **quarantine**. El
`ConnectorState` puede marcar `parser_broken`. El último-bueno **no** se toca: la
lectura de precio actual sigue devolviendo el dato previo.

**Acción:** corregir el parser (subir `parser_version`), luego
`reprocess_capture --capture-id <uuid>` sobre capturas recientes (se guardaron con
retención `medium`/`extended`) o `sync_retailer` para un run nuevo. Revisar y cerrar
la cuarentena.

---

## 2. Página de CAPTCHA / bloqueo

**Síntoma:** la fuente devuelve un interstitial anti-bot o CAPTCHA.

**Comportamiento del sistema:** `detect_block_page()` lo marca (`is_block_page`);
**nunca** intenta resolverlo. La observación se cuarentena (`BLOCK_PAGE`, critical),
la captura se guarda con retención `extended` y el `ConnectorState` pasa a
`temporarily_blocked` (o `permission_required` si es un bloqueo estructural). **No se
evade.**

**Acción:** ninguna técnica de evasión, por política. Si es estructural (la fuente
prohíbe el acceso), marcar `permission_required` y dejar el conector desactivado (ver
[`SCRAPING_POLICY.md`](SCRAPING_POLICY.md)). Si fue un bloqueo transitorio por ritmo,
esperar el enfriado del circuito; revisar los límites `SCRAPING_*`.

---

## 3. 403 / 429 / 500 de la fuente

**Comportamiento del sistema:** 500/502/503/504 son reintentables → backoff
exponencial + jitter en el `HttpFetcher`. 403/429 se tratan como posible bloqueo
(`is_block_page`) y **no** se reintentan a lo bruto. Tras
`CONNECTOR_FAILURE_THRESHOLD` (5) fallos consecutivos por dominio, el **circuit
breaker** abre el circuito `CONNECTOR_CIRCUIT_OPEN_MINUTES` (30) y los fetches
cortocircuitan. En la cola, el job hace backoff y, agotado, `dead_letter`.

**Acción:** `connector_health` para ver el circuito; esperar el enfriado. Si es
persistente, bajar la agresividad (`SCRAPING_MAX_CONCURRENCY`, subir los delays) o
reevaluar si la fuente debe quedar `permission_required`.

---

## 4. Catálogo cae un 90 %

**Comportamiento del sistema:** `catalog_drop` (critical) → el lote se **cuarentena
entero**; el último-bueno se conserva. La cobertura del snapshot lo reflejará, pero
los precios buenos siguen sirviéndose.

**Acción:** investigar si la caída es real (fuente redujo catálogo) o un fallo de
descubrimiento/parseo. Si es un artefacto, corregir y re-sincronizar; aprobar la
cuarentena sólo si la caída es legítima.

---

## 5. Un precio 5,49 → 549 (×100)

**Comportamiento del sistema:** el detector marca `price_x100` como `price_spike`
critical (ratio ≈ 100); además la validación de coherencia del precio unitario (tol.
2 %) atrapa el desliz. La observación afectada se **cuarentena por variante**; el
resto del lote se acepta. El precio bueno anterior permanece.

**Acción:** revisar la anomalía (`GET /api/v1/admin/anomalies`); rechazarla si es un
error de origen/parseo. No hace falta tocar el último-bueno: no fue reemplazado.

---

## 6. El cron se dispara dos veces (doble programación)

**Comportamiento del sistema:** el scheduler toma un **advisory lock** de Postgres
(`pg_try_advisory_xact_lock`); el segundo proceso no lo adquiere y sale sin hacer
nada. Aun sin eso, la **idempotencia** por `(retailer, store, run_type, fecha)` y por
`idempotency_key` en la cola evita duplicar jobs.

**Acción:** ninguna. Es seguro por diseño; también frente a una ejecución manual
solapada.

---

## 7. Muere el worker a mitad de un job

**Comportamiento del sistema:** el job queda `locked` con heartbeat obsoleto. Al
reiniciar el worker (`ON_FAILURE` en Railway), `recover_stuck_jobs()` lo devuelve a
`queued` **sin consumir intento** (la muerte del worker no es culpa del job). Un job
que agotó reintentos legítimos está en `dead_letter`.

**Acción:** normalmente ninguna. Para dead-letters, corregir la causa y
`retry_failed --run-id <uuid>`.

---

## 8. Una tienda no resuelve

**Comportamiento del sistema:** `resolve_store()` devuelve baja `confidence`; la
tienda se **omite** en vez de inventar un ámbito. El conector puede quedar `degraded`.
No se crean precios `exact_store` sin tienda resuelta (regla de validación §7).

**Acción:** revisar la configuración de la tienda (`Store`, código postal, id
externo). Re-lanzar con `sync_store --store-id <uuid>` una vez corregida.

---

## 9. Cobertura parcial

**Comportamiento del sistema:** `CoverageSnapshot` reporta el estado **honesto**
(`partial`, `insufficient`, `stale` o `none`), nunca `complete` si no lo es. La API de
consumo expone ese estado a la aplicación.

**Acción:** ninguna urgente; es información veraz. Mejorar cobertura pasa por añadir
fuentes legales o datos (ver [`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md)).

---

## 10. Precio con más de 24 h / 48 h

**Comportamiento del sistema:** `CurrentPriceService` marca `stale` (≥ 24 h) o
`expired` (≥ 48 h) según `STALE_PRICE_HOURS` / `EXPIRED_PRICE_HOURS`. El precio se
sigue devolviendo, pero **etiquetado** con su frescura; no se descarta silenciosamente
ni se sustituye por un valor inventado.

**Acción:** si mucho catálogo está `stale`/`expired`, revisar por qué no llegan runs
frescos (scheduler parado, circuito abierto, conector desactivado). `connector_health`
y `GET /api/v1/admin/crawls`.

---

## 11. Un conector falla y los demás funcionan

**Comportamiento del sistema:** **aislamiento por job**. Cada job se procesa en su
propio `try` (`process_job`); una excepción sólo falla **ese** job (backoff /
dead-letter) y afecta al `ConnectorState` de **su** retailer. El worker sigue y los
demás retailers se procesan con normalidad. Un retailer sin conector registrado cae en
un no-op seguro.

**Acción:** diagnosticar el conector afectado (`connector_health`, último error);
corregir y `retry_failed` / `sync_retailer`. No es necesario tocar el resto.

---

## Resumen: seguro por defecto

| Escenario | Mecanismo automático | ¿Se pierde el último-bueno? |
|-----------|----------------------|:---------------------------:|
| Parser roto | `parser_returned_zero` → cuarentena, `parser_broken` | No |
| CAPTCHA / bloqueo | detectar → parar → `temporarily_blocked` (nunca evadir) | No |
| 403/429/500 | backoff → circuit breaker | No |
| Catálogo −90 % | `catalog_drop` → cuarentena de lote | No |
| Precio ×100 | `price_x100` → cuarentena por variante | No |
| Doble cron | advisory lock + idempotencia | No |
| Muerte del worker | heartbeat + `recover_stuck_jobs` + dead-letter | No |
| Tienda no resuelve | baja confianza → omitir | No |
| Cobertura parcial | `CoverageSnapshot` honesto | No |
| Precio viejo | `stale` / `expired` | No |
| Un conector cae | aislamiento por job | No |
