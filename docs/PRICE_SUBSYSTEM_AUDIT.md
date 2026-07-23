# Auditoría de producción — Subsistema de ingesta de precios

> Fecha de la auditoría: 2026-07-23 · Suite: **547/547** · Los 11 escenarios de fallo
> verificados por tests que pasan. Sin scraping activo, sin invención de datos, cobertura
> y ámbito siempre honestos. Documentos relacionados: [`PRICE_INGESTION.md`](PRICE_INGESTION.md),
> [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md), [`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md),
> [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md), [`PRICE_QUALITY.md`](PRICE_QUALITY.md),
> [`FASE_F_DEPLOYMENT.md`](FASE_F_DEPLOYMENT.md).

## Metodología

Para cada escenario de fallo se ejecutó su(s) test(s) específico(s) del subsistema
(`tests/ingestion/`). Todos pasan; no hubo fallos que corregir. Reproducible con:

```bash
cd apps/api
uv run pytest tests/ingestion -k "test_parser_returned_zero_flagged or test_block_page_flagged_critical or \
  test_block_page_detection_does_not_attempt_to_solve or test_retries_on_500_then_succeeds or \
  test_circuit_breaker_opens_after_threshold_and_short_circuits or test_block_page_flagged_on_403 or \
  test_block_page_flagged_on_429 or test_catalog_drop_90pct_flagged or test_price_x100_flagged_critical or \
  test_schedule_daily_is_idempotent or test_recover_stuck_jobs_requeues_stale_heartbeat or \
  test_fail_job_backoff_then_dead_letter or test_resolve_store_maps_osm_location_to_exact_store or \
  test_schedule_daily_skips_blocked_connector or test_partial_coverage_reports_partial or \
  test_current_status_stale_then_expired or test_all_stale_coverage_is_stale or \
  test_failing_job_is_isolated_others_still_process or test_process_job_isolates_exception or \
  test_connector_circuit_opens_after_threshold or test_quarantined_does_not_replace_last_good" -v
# -> 21 passed
```

## Escenarios de fallo (evidencia)

| # | Escenario | Comportamiento del sistema | Test (PASSED) |
|---|---|---|---|
| 1 | **Cambia el HTML** | El parser devuelve 0 → anomalía `parser_zero` → **cuarentena**; conector → `parser_broken`; no reemplaza el último bueno | `test_parser_returned_zero_flagged` |
| 2 | **CAPTCHA** | `HttpFetcher` la **detecta y reporta** (`is_block_page`), **nunca la resuelve**; validación falla → cuarentena | `test_block_page_flagged_critical`, `test_block_page_detection_does_not_attempt_to_solve` |
| 3 | **403 / 429 / 500** | Reintentos con backoff+jitter (500); 403/429 marcados como bloqueo; tras N fallos → **circuit breaker** abre y corta | `test_retries_on_500_then_succeeds`, `test_circuit_breaker_opens_after_threshold_and_short_circuits`, `test_block_page_flagged_on_403`, `test_block_page_flagged_on_429` |
| 4 | **Catálogo −90 %** | Anomalía `catalog_drop` → cuarentena; datos previos intactos | `test_catalog_drop_90pct_flagged` |
| 5 | **5,49 € → 549 € (×100)** | Anomalía `price_x100` **crítica** → cuarentena; la observación buena sigue vigente | `test_price_x100_flagged_critical` |
| 6 | **Doble cron** | Advisory-lock + idempotencia por retailer/store/fecha → no duplica | `test_schedule_daily_is_idempotent` |
| 7 | **Muere un worker** | Heartbeat + `recover_stuck_jobs` re-encola; `dead_letter` tras `max_attempts` con backoff | `test_recover_stuck_jobs_requeues_stale_heartbeat`, `test_fail_job_backoff_then_dead_letter` |
| 8 | **Tienda no resuelve** | `StoreResolution` con ámbito/confianza explícitos; conector bloqueado se salta | `test_resolve_store_maps_osm_location_to_exact_store`, `test_schedule_daily_skips_blocked_connector` |
| 9 | **Cobertura parcial** | `CoverageSnapshot` con estado **honesto** (`partial`), nunca "completo" | `test_partial_coverage_reports_partial` |
| 10 | **Precio >24/48 h** | `CurrentPriceService` marca `stale` (>24 h) / `expired` (>48 h) | `test_current_status_stale_then_expired`, `test_all_stale_coverage_is_stale` |
| 11 | **Un conector falla, otros siguen** | Aislamiento por job; el fallo no detiene los demás; su circuito abre | `test_failing_job_is_isolated_others_still_process`, `test_process_job_isolates_exception`, `test_connector_circuit_opens_after_threshold` |
| + | **Anomalía no reemplaza el último bueno** | La observación en cuarentena se guarda como `disputed`; el precio bueno sigue abierto | `test_quarantined_does_not_replace_last_good` |

## 1. Riesgos restantes

- **No existe fuente densa legal**: los precios reales completos por cadena **no son
  accesibles legalmente** (robots.txt de las grandes prohíbe sus endpoints; sin feed
  oficial). El planificador solo costea al 100 % con **catálogo importado / feed comercial**
  (`csv_feed`) o la demo.
- **Solo 3 conectores operan** hoy (`demofixturemart`, `open_prices`, `csv_feed`); los de
  cadena y ofertas están `permission_required`/`unsupported`.
- **`observed_at` re-sellado al `as_of`** del run en `run_price_sync` (diseño de "instante
  efectivo"); el conector guarda la fecha real de la fuente.
- **Playwright no integrado** (innecesario para los conectores actuales; sería para futuros
  que requieran contexto, y solo si su fuente lo permite legalmente).
- **3 warnings** de deprecación de dependencias en la suite (no fallos) — deuda menor.
- **Operación no ejecutada en Railway** aún: FASE F documentada ([`FASE_F_DEPLOYMENT.md`](FASE_F_DEPLOYMENT.md)),
  el despliegue lo lanza el operador.

## 2. Limitaciones por supermercado

| Cadena | Estado | Motivo |
|---|---|---|
| Mercadona / Carrefour / Lidl (catálogo) | `permission_required` | su `robots.txt` **prohíbe** sus endpoints de datos (`/api`, `/supermercado/ajax`, `/user-api`) |
| Lidl / Aldi (ofertas) | `permission_required` | sin fuente pública accesible sin autorización; conector implementado, no operable |
| Dia | `partial_only` / `permission_required` | solo páginas de catálogo parciales; Club Dia ≠ precio general |
| Deza | `unsupported` (scraping) → **importación** | pequeña regional; vía CSV/JSON autorizado o entrada manual |
| Open Prices | **active** | dataset abierto **ODbL** (real, escaso) |
| CsvFeed (operador) | **active** | dato aportado por el operador con derechos |
| Demo | **active** | fixtures sintéticas |

## 3. Cobertura real

- **Real hoy**: `open_prices` (ODbL, **escaso** — pocos productos por tienda y cubre
  productos que las recetas no compran) + `csv_feed` (**denso** si el operador aporta un
  catálogo con derechos).
- **Coste 100 % de un plan**: verificado solo con **catálogo denso** (la demo o un import
  denso). Las cadenas reales dan cobertura ~0 % con Open Prices — mostrado honestamente
  (`price_coverage`, split conocido/estimado, lista de no-resueltos).

## 4. Conectores que requieren autorización

- **Mercadona, Carrefour, Lidl** (catálogo) y **Lidl/Aldi** (ofertas): `permission_required`.
  Se activan **solo** al cargar el artefacto de autorización → `DataSource.legal_status =
  authorized` (feed oficial con credenciales → `authorized_feed`/`csv_feed`, o permiso escrito
  documentado). Habilitar uno sin evidencia devuelve **HTTP 409** por diseño.
- **Deza**: vía importación / feed autorizado, no scraping.

## 5. Acciones antes de producción

1. **Aportar una fuente densa legal** (catálogo con derechos vía `csv_feed`/import, o feed
   comercial contratado) — es lo único que desbloquea el coste real por cadena.
2. **Desplegar** `ingestion-scheduler` + `ingestion-worker` en Railway según
   [`FASE_F_DEPLOYMENT.md`](FASE_F_DEPLOYMENT.md); aplicar `alembic upgrade head`.
3. **Mantener flags apagados** (`SCRAPING_ENABLED=false`, `PRICE_SYNC_ENABLED=false`,
   `*_CONNECTOR_ENABLED=false`) y habilitar **solo** `open_prices`/`csv_feed` vía API admin.
4. **Primera ejecución controlada pequeña** (`sync_retailer`/`sync_store`, no catálogo
   completo) + verificar `connector_health` y cobertura.
5. **Alertas** sobre `consecutive_failures`, `circuit_open`, caída de cobertura, ratio
   `stale/expired` y jobs en `dead_letter` (expuesto por la API admin).
6. **Limpiar los 3 warnings** de deprecación y revisar retención/secretos
   ([`DATA_RETENTION.md`](DATA_RETENTION.md)).
7. **No activar ningún conector de cadena sin autorización documentada.**

## Veredicto

El subsistema es **seguro por defecto, honesto y resiliente**: los 11 escenarios de fallo
están cubiertos y verificados por tests; no inventa datos, no scrapea fuentes bloqueadas, no
presenta cobertura parcial como completa y aísla fallos por conector. El **único bloqueo para
producción es de datos** (disponer de una fuente densa legal), no de ingeniería.
