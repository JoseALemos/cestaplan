# FASE F — Playbook de despliegue operativo de la ingesta de precios

Guía **operativa** para poner en producción y operar el subsistema de ingesta de
precios de NutriPlan/CestaPlan (scheduler + worker de rastreo, conectores, calidad,
cobertura y consola de administración). No es una guía de arquitectura: para eso están
[`PRICE_INGESTION.md`](PRICE_INGESTION.md), [`CONNECTOR_ARCHITECTURE.md`](CONNECTOR_ARCHITECTURE.md)
y [`ARCHITECTURE.md`](ARCHITECTURE.md).

Documentos que este playbook **referencia en vez de duplicar**:
[`DEPLOYMENT.md`](DEPLOYMENT.md) (despliegue base de `api`/`web`/`worker`),
[`RAILWAY_PRICE_SYNC.md`](RAILWAY_PRICE_SYNC.md) (los dos servicios de ingesta en Railway),
[`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) (runbook de incidentes),
[`PRICE_QUALITY.md`](PRICE_QUALITY.md) (frescura, cobertura, anomalías),
[`DATA_RETENTION.md`](DATA_RETENTION.md) (retención de capturas),
[`SCRAPING_POLICY.md`](SCRAPING_POLICY.md) (política de acceso a fuentes) y
[`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md) (estado honesto por fuente).

> **El asistente no despliega.** Todos los pasos de dashboard, secretos y ejecución de
> comandos los realiza el **operador**. Este documento describe qué hacer y en qué orden.

---

## 1. Alcance y estado honesto

**FASE F = operar el subsistema en producción.** El código de ingesta (FASES A–E) ya
existe: cola en Postgres, scheduler idempotente, worker de rastreo con aislamiento por
job, circuit breaker, cuarentena de anomalías, cobertura honesta, consola de admin y
tres conectores reales implementados. Esta fase consiste en **desplegar los dos
servicios que faltan, activar sólo lo que es legal, y operar con seguridad** — sin
scraping y sin fabricar precios.

### Qué conectores pueden funcionar hoy

| Conector (`retailer_code`) | Tipo | ¿Puede operar en vivo? | Base |
|----------------------------|------|:----------------------:|------|
| `demofixturemart` | Demo sintético, sin red | **Sí** (siempre registrado) | Dato sintético; existe para ejercitar el vertical completo. |
| `open_prices` | `open_dataset` real (ODbL) | **Sí** | Dataset abierto Open Prices (Open Food Facts), con atribución ODbL. Única fuente de **precios reales** activa. |
| `csv_feed` | Feed de operador (CSV/JSON) | **Sí** | Un feed que el operador **aporta legítimamente** (su catálogo, un feed licenciado, tickets). No es scraping. |

### Qué conectores NO pueden activarse

Los conectores de **cadenas** (`mercadona`, `carrefour`) y de **ofertas**
(`lidl_offers`, `aldi_offers`, `deza`) están **implementados como framework pero
desactivados** y **nunca rastrean**. Su base legal es `permission_required` (o
`unsupported` en el caso de scraping de Deza), por lo que **no pueden activarse para
correr en vivo** hasta que exista autorización explícita de la cadena o un feed
autorizado que el operador aporte al conector. Intentar activarlos por la API devuelve
**409** por diseño (ver §5). El detalle por fuente está en
[`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md).

> Regla transversal (ver [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md)): `robots.txt` no
> es una autorización, una fuente bloqueada se **detiene y se marca**, nunca se evade, y
> las promociones se modelan (con fechas de validez) en vez de colapsarse a un precio.

---

## 2. Requisitos previos

1. **Postgres** en marcha (el mismo que usan `api` y `worker`; no hay Redis). La cola de
   rastreo (`CrawlJob`/`CrawlRun`) y todo el historial viven en Postgres.
2. **`api` y `worker` (de planes) ya desplegados** según [`DEPLOYMENT.md`](DEPLOYMENT.md).
   El subsistema de ingesta reutiliza la **imagen del `api`** (`apps/api/Dockerfile`).
3. **Migraciones aplicadas** (ver §4): las tablas de ingesta las crea la migración
   `1f9cf8405c1b` (FASE A).
4. **Variables de entorno** de ingesta configuradas (tabla siguiente). Todas viven en
   `.env.example`; en Railway se fijan por servicio y entorno.

### Variables de entorno de la ingesta

Todas se leen desde `.env.example`. Ninguna de estas es un secreto (los únicos secretos
del proyecto son `SESSION_SECRET` y, si aplica, `OPENAI_API_KEY` / claves de feed).

| Variable | Propósito | Por defecto | ¿Secreto? |
|----------|-----------|:-----------:|:---------:|
| `DATABASE_URL` | Conexión a Postgres (en Railway, `${{Postgres.DATABASE_URL}}` por red privada) | `postgresql+psycopg://…localhost` | No (es una referencia) |
| `SCRAPING_ENABLED` | Interruptor maestro de la capa HTTP de scraping | `false` | No |
| `PRICE_SYNC_ENABLED` | Interruptor maestro de la sincronización de precios | `false` | No |
| `SCRAPING_USER_AGENT` | User-Agent honesto e identificable | `CestaPlanBot/0.0 (+…)` | No |
| `SCRAPING_CONTACT_EMAIL` | Contacto de abuso/consultas | *(vacío)* | No |
| `SCRAPING_MAX_CONCURRENCY` | Peticiones en vuelo por dominio | `2` | No |
| `SCRAPING_REQUEST_DELAY_MIN_MS` | Retardo mínimo entre peticiones al mismo dominio | `500` | No |
| `SCRAPING_REQUEST_DELAY_MAX_MS` | Retardo máximo (jitter) | `1500` | No |
| `SCRAPING_TIMEOUT_SECONDS` | Timeout por petición | `20` | No |
| `SCRAPING_MAX_RETRIES` | Reintentos HTTP | `3` | No |
| `SCRAPING_MAX_RESPONSE_MB` | Aborta descargas mayores | `5` | No |
| `RAW_CAPTURE_RETENTION_DAYS` | Horizonte de `RawCapture.expires_at` | `30` | No |
| `STALE_PRICE_HOURS` | Umbral de precio `stale` | `24` | No |
| `EXPIRED_PRICE_HOURS` | Umbral de precio `expired` | `48` | No |
| `CONNECTOR_FAILURE_THRESHOLD` | Fallos consecutivos antes de abrir el circuito | `5` | No |
| `CONNECTOR_CIRCUIT_OPEN_MINUTES` | Minutos que el circuito de un dominio queda abierto | `30` | No |
| `WORKER_POLL_INTERVAL_SECONDS` | Sondeo de la cola | `2` | No |
| `WORKER_JOB_MAX_ATTEMPTS` | Máximo de intentos por job | `3` | No |
| `WORKER_HEARTBEAT_SECONDS` | Latido del worker sobre el job en curso | `15` | No |
| `MERCADONA_CONNECTOR_ENABLED` | Flag del conector Mercadona | `false` | No |
| `ALCAMPO_CONNECTOR_ENABLED` | Flag del conector Alcampo | `false` | No |
| `CARREFOUR_CONNECTOR_ENABLED` | Flag del conector Carrefour | `false` | No |
| `DIA_CONNECTOR_ENABLED` | Flag del conector Dia | `false` | No |
| `LIDL_OFFERS_CONNECTOR_ENABLED` | Flag del conector de ofertas Lidl | `false` | No |
| `ALDI_OFFERS_CONNECTOR_ENABLED` | Flag del conector de ofertas Aldi | `false` | No |
| `DEZA_CONNECTOR_ENABLED` | Flag del conector Deza | `false` | No |

> **Todos los `*_CONNECTOR_ENABLED` arrancan en `false`.** Y aunque se pongan en `true`,
> los conectores `permission_required`/`unsupported` **siguen sin rastrear**: el flag no
> anula la política legal (ver §5 y `RETAILER_SOURCE_MATRIX.md`). El interruptor maestro
> `PRICE_SYNC_ENABLED`/`SCRAPING_ENABLED` también empieza apagado.

---

## 3. Servicios Railway a añadir

La ingesta añade **dos** servicios, ambos sin dominio público y sin healthcheck (no
atienden HTTP), que reutilizan la imagen del `api`. Son distintos del `worker` de planes
(`cestaplan_worker.main`) y del cron `open-prices-sync`. Detalle completo en
[`RAILWAY_PRICE_SYNC.md`](RAILWAY_PRICE_SYNC.md) y el mapa en
[`infra/railway/README.md`](../infra/railway/README.md).

| Servicio | Config file | Dockerfile | startCommand | Tipo | Dominio | Restart |
|----------|-------------|------------|--------------|------|:-------:|---------|
| `ingestion-worker` | [`infra/railway/ingestion-worker.json`](../infra/railway/ingestion-worker.json) | `apps/api/Dockerfile` | `python -m cestaplan_api.jobs.crawl_worker` | Demonio | **No** | `ON_FAILURE` (máx. 10) |
| `ingestion-scheduler` | [`infra/railway/ingestion-scheduler.json`](../infra/railway/ingestion-scheduler.json) | `apps/api/Dockerfile` | `python -m cestaplan_api.jobs.schedule_daily_price_sync` | **Cron diario** `0 3 * * *` (UTC) | **No** | `NEVER` |

### Pasos en el dashboard (por cada uno de los dos servicios)

1. Crea el servicio en el proyecto/entorno (`staging` o `production`) y conéctalo al repo.
2. **Deja el *Root Directory* en la raíz del repo** (vacío / `/`). Fijarlo a `apps/api`
   rompería los `COPY` del Dockerfile (ver la nota de contexto de build en
   [`infra/railway/README.md`](../infra/railway/README.md)).
3. En *Settings → Config-as-code → Railway config file*, apunta a:
   - `ingestion-worker` → `infra/railway/ingestion-worker.json`
   - `ingestion-scheduler` → `infra/railway/ingestion-scheduler.json`
   (El JSON ya fija Dockerfile, `startCommand`, `cronSchedule` y `restartPolicyType`.)
4. **Variables de entorno:** referencia `DATABASE_URL` como `${{Postgres.DATABASE_URL}}`
   (**red privada**, nunca la URL pública) y añade las variables de §2 según necesites
   afinar la ingesta. Estos servicios **no** deben tener secretos que no necesiten.
5. **No** añadas dominio público ni healthcheck a ninguno de los dos.
6. El **cron** (`ingestion-scheduler`) corre en **UTC**; `0 3 * * *` = 03:00 UTC. Ajusta
   la franja si prefieres el valle nocturno de una fuente; el subsistema no depende de una
   hora concreta.

> **¿Playwright?** **No** hace falta para los conectores activos hoy (`demofixturemart`,
> `open_prices`, `csv_feed`). Sólo se añadiría un navegador Playwright a la imagen **si y
> cuando** un conector futuro lo requiriese para renderizar contexto legítimo. De momento,
> la imagen del `api` basta.

---

## 4. Migraciones

Las tablas de ingesta las crea la migración **FASE A** (`revision 1f9cf8405c1b`,
`fase_a_ingestion_foundation`). Se aplican por el **pre-deploy command** del servicio
`api` (`alembic upgrade head`, ya configurado en `infra/railway/api.json`), que corre
**una sola vez por deploy** y **nunca** en los workers.

```bash
# Manual (una vez), desde una shell del servicio api o local con el DATABASE_URL correcto:
alembic upgrade head
```

Confirma que existen las tablas de ingesta creadas por esa migración:

```
connector_state   coverage_snapshot   crawl_job        crawl_run
external_product  price_anomaly       price_observation product_variant
promotion_rule    raw_capture         store_resolution
```

> `ingestion-worker` y `ingestion-scheduler` **no** ejecutan migraciones: sólo el `api`.

---

## 5. Activación segura de conectores

### 5.1 Crear un administrador

`make_admin` marca `is_admin=True` sobre un usuario **que ya existe** (regístralo antes
por el flujo normal de la app). El email se normaliza a minúsculas:

```bash
python -m cestaplan_api.scripts.make_admin <email>
# p. ej.:
python -m cestaplan_api.scripts.make_admin admin@example.com
```

### 5.2 Activar SÓLO conectores permitidos

La consola de admin (prefijo `/api/v1/admin`, requiere admin + CSRF en mutaciones) sólo
mueve un conector `disabled ↔ active` y **rechaza con 409** cualquier conector cuya base
legal sea `permission_required`/`prohibited` o cuyo estado sea `unsupported`:

```bash
# Activar los conectores que SÍ pueden operar:
POST /api/v1/admin/connectors/open_prices/enable
POST /api/v1/admin/connectors/csv_feed/enable
POST /api/v1/admin/connectors/demofixturemart/enable   # demo: siempre operable

# Deshabilitar (idempotente):
POST /api/v1/admin/connectors/{code}/disable
```

Intentar activar una cadena o un conector de ofertas devuelve **409 por diseño**:

```bash
POST /api/v1/admin/connectors/mercadona/enable   # → 409 (permission_required)
POST /api/v1/admin/connectors/lidl_offers/enable # → 409 (permission_required)
POST /api/v1/admin/connectors/deza/enable        # → 409 (unsupported / permission_required)
```

### 5.3 Configurar las DataSource

- **Open Prices:** su `DataSource` (slug `open-prices`, `adapter_key=open_prices`,
  `source_type=open_dataset`, ODbL) se asegura automáticamente y arranca `is_enabled=true`;
  un admin que lo deshabilite mantiene el control (no se sobreescribe). Se puede sembrar/
  refrescar con el seed de tiendas reales (ver §6.1). Su footing legal por defecto no está
  en la lista bloqueada, por lo que el conector **sí** se puede activar.
- **csv_feed:** requiere un `DataSource` del feed que aporta el operador (por defecto slug
  `operator-feed`, footing `authorized`). El conector lee el feed desde contenido, un
  `feed_path` o un `feed_url`, y **sólo** produce observaciones desde ese feed. Habilita su
  `DataSource.is_enabled=true` para que corra.

> El paper trail de cumplimiento por fuente (footing legal, `terms_reviewed_at`,
> `robots_reviewed_at`, notas) es consultable en `GET /api/v1/admin/sources`.

---

## 6. Primera ejecución controlada

El objetivo es una prueba **pequeña**, no un catálogo completo.

### 6.1 Sembrar / verificar tiendas

```bash
# Demo (retailer + tienda sintéticos + catálogo de ~26 productos):
python -m cestaplan_api.scripts.seed_demo

# Tiendas reales de Open Prices (ES) desde la lista embebida (sin red);
# usa --discover para descubrir en vivo TODAS las tiendas ES (consulta /locations):
python -m cestaplan_api.scripts.seed_open_prices_stores
```

### 6.2 Una ejecución manual PEQUEÑA (no el catálogo entero)

`sync_retailer` fuerza la programación de un retailer ahora (ignora freshness);
`sync_store` lo hace para **una** tienda por su `public_id` (UUID). Empieza por una:

```bash
# Un solo retailer real:
python -m cestaplan_api.jobs.sync_retailer --retailer open_prices

# …o mejor aún, una sola tienda (lo más acotado):
python -m cestaplan_api.jobs.sync_store --store-id <uuid>
```

Salida esperada (ejemplo):

```
CestaPlan — sincronización forzada del retailer open_prices
  runs_creados=1 jobs_creados=1
    run 4f3c…-uuid
```

### 6.3 Arrancar el worker y verlo drenar

En Railway el `ingestion-worker` ya está corriendo; en local:

```bash
python -m cestaplan_api.jobs.crawl_worker
```

El worker toma jobs con `SELECT … FOR UPDATE SKIP LOCKED`, procesa uno a la vez, emite
heartbeats y actualiza `ConnectorState`. Observa la cola vaciarse en
`GET /api/v1/admin/crawls` (el run pasa a `completed`) o por logs del servicio.

---

## 7. Verificación

```bash
# Salud de conectores (estado, fallos consecutivos, circuito, último error):
python -m cestaplan_api.jobs.connector_health
```

Vía API de administración (admin autenticado):

```bash
GET /api/v1/admin/connectors            # estado + footing legal por conector
GET /api/v1/admin/connectors/{code}     # detalle: estado, capacidades, último run, cobertura
GET /api/v1/admin/coverage              # snapshots de cobertura por retailer/tienda
```

Superficie de consumo (sesión autenticada; prefijo `/api/v1`):

```bash
GET  /api/v1/stores/{id}/catalog-status # discovered/priced/fresh/stale + último run
GET  /api/v1/prices/current?variant_id=<uuid>[&store_id=<uuid>][&scope=…]
POST /api/v1/prices/resolve-basket      # smoke: coste de una cesta (store_id o retailer_id + items)
```

**Qué es "sano":**
- Cobertura **fresca** en `/coverage` con un `status` honesto (`complete`/`high`/`partial`
  según toque) y `fresh_prices` > 0 en las tiendas ejecutadas.
- **Sin circuito abierto** (`circuit_open_until` nulo) y `consecutive_failures` bajo en
  `connector_health` / `/connectors`.
- **Sin anomalías críticas sin revisar** en `GET /api/v1/admin/anomalies`.
- `resolve-basket` devuelve un coste con `unresolved` honesto (los faltantes se listan,
  **nunca** se fabrican precios).

---

## 8. Monitorización

**Health checks:**
- `api`: `GET /health` responde `200` (retrasa el enrutado hasta que el deploy está sano).
- `postgres`: `pg_isready` (gestionado por Railway).
- `worker` (planes) e `ingestion-worker`: **sin healthcheck HTTP**; se vigilan por logs
  (heartbeats, sin errores de conexión) y por el avance de sus colas.
- `ingestion-scheduler`: cron; verifica que corre a diario y que crea runs (idempotente).
- Por conector: `python -m cestaplan_api.jobs.connector_health` y `GET /api/v1/admin/connectors`.

**Cadencia diaria:** el scheduler crea los `CrawlRun`+`CrawlJob` del día una vez (03:00
UTC por defecto), con freshness por `(retailer, store, run_type)` en días (ver
[`PRICE_QUALITY.md`](PRICE_QUALITY.md) para frescura/cobertura). No programa retailers
`disabled`/`unsupported`/`permission_required` ni con el circuito abierto.

**Qué alertar:**
- `consecutive_failures` creciente o `ConnectorState = temporarily_blocked` / `parser_broken`.
- `circuit_open_until` no nulo de forma persistente (circuito abierto).
- Caída de cobertura (`coverage_drop`, ≥ 0.3 respecto al día previo) o `status` que baja a
  `insufficient`/`stale`/`none`.
- Ratio alto de precios `stale` (≥ 24 h) / `expired` (≥ 48 h) → runs frescos no llegan.
- Jobs en `dead_letter` (reintentos agotados) y anomalías `critical` sin revisar.

---

## 9. Recuperación de fallos

Runbook completo en [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md). Atajos por escenario:

| Escenario (INCIDENT_RESPONSE) | Comando / acción |
|-------------------------------|------------------|
| §1 Parser roto | Corregir parser (subir `parser_version`) → `python -m cestaplan_api.jobs.reprocess_capture --capture-id <uuid>` sobre capturas recientes; revisar cuarentena. |
| §3 403/429/500, circuito abierto | `python -m cestaplan_api.jobs.connector_health`; esperar el enfriado; bajar agresividad (`SCRAPING_*`). |
| §5 Precio ×100 / anomalía | `GET /api/v1/admin/anomalies` → `POST …/anomalies/{id}/approve` o `/reject` (sólo saca de cuarentena; **no** toca el último-bueno). |
| §7 Muerte del worker | Railway reinicia (`ON_FAILURE`); al arrancar, `recover_stuck_jobs()` re-encola los jobs con heartbeat obsoleto **sin consumir intento**. |
| §7 Jobs en dead-letter | Corregir la causa → `python -m cestaplan_api.jobs.retry_failed --run-id <uuid>`. |
| §8 Tienda no resuelve | Corregir `Store` → `python -m cestaplan_api.jobs.sync_store --store-id <uuid>`. |
| Cancelar un crawl en curso | `POST /api/v1/admin/crawls/{crawl_id}/cancel` (cancela el run no terminal y sus jobs pendientes). Reintentar sus fallidos: `POST /api/v1/admin/crawls/{crawl_id}/retry`. |

---

## 10. Rollback / desactivar

El motor de comidas **no depende** de la ingesta en vivo: sigue funcionando sobre las
proyecciones `ProductPrice` y los catálogos demo/importados. Para parar la ingesta sin
romper la app, en orden creciente de contundencia:

1. **Interruptor maestro:** `PRICE_SYNC_ENABLED=false` (y/o `SCRAPING_ENABLED=false`).
2. **Por conector:** `POST /api/v1/admin/connectors/{code}/disable` o poner su
   `*_CONNECTOR_ENABLED=false` / `DataSource.is_enabled=false`.
3. **Parar la infraestructura:** detén el `ingestion-worker` y el `ingestion-scheduler`
   en Railway (o sus procesos en autohospedado). La cola queda en Postgres, intacta, para
   reanudar después. El historial de precios es append-only y no se pierde.

---

## 11. Autohospedado (sin Railway)

El subsistema no depende de Railway. Los dos servicios son procesos del mismo paquete
(imagen del `api`). Detalle en [`RAILWAY_PRICE_SYNC.md`](RAILWAY_PRICE_SYNC.md#5-equivalente-autohospedado).

### Nativo (systemd / supervisor + cron del sistema)

```bash
# Worker (demonio):
python -m cestaplan_api.jobs.crawl_worker

# Scheduler (cron del sistema, p. ej. crontab a las 03:00 UTC):
0 3 * * *  cd /app/apps/api && python -m cestaplan_api.jobs.schedule_daily_price_sync
```

### docker-compose

El `docker-compose.yml` del repo ya define `api`, `web`, `worker` y `postgres`;
`ingestion-worker` sigue el mismo patrón que `worker`, cambiando el `command`:

```yaml
services:
  ingestion-worker:
    image: cestaplan-api          # misma imagen que el servicio api
    command: python -m cestaplan_api.jobs.crawl_worker
    environment:
      DATABASE_URL: ${DATABASE_URL}
    restart: on-failure
  # El scheduler se lanza por el cron del host (o un sidecar tipo ofelia):
  #   docker compose run --rm ingestion-worker \
  #     python -m cestaplan_api.jobs.schedule_daily_price_sync
```

---

## 12. Checklist pre-producción

- [ ] Migraciones aplicadas (`alembic upgrade head`); tablas de ingesta presentes (§4).
- [ ] `ingestion-worker` desplegado (config file, sin dominio, `ON_FAILURE`).
- [ ] `ingestion-scheduler` desplegado (config file, cron `0 3 * * *` UTC, `NEVER`).
- [ ] `DATABASE_URL` referenciado por **red privada** (`${{Postgres.DATABASE_URL}}`) en ambos.
- [ ] Todos los `*_CONNECTOR_ENABLED` en `false` salvo lo que se vaya a operar; interruptor
      maestro (`PRICE_SYNC_ENABLED`/`SCRAPING_ENABLED`) revisado.
- [ ] Administrador creado (`make_admin <email>`).
- [ ] Activados **sólo** `demofixturemart` / `open_prices` / `csv_feed`; ninguna cadena ni
      conector de ofertas activado (devuelven 409 por diseño).
- [ ] Primera ejecución **pequeña** OK (`sync_store`/`sync_retailer` + worker drenando).
- [ ] Health en verde: `connector_health`, `/connectors`, `/coverage` con cobertura fresca,
      sin circuito abierto, sin anomalías críticas sin revisar.
- [ ] Alertas configuradas (§8): fallos consecutivos, circuito, caída de cobertura,
      stale/expired, dead-letter.
- [ ] Retención de capturas revisada (`RAW_CAPTURE_RETENTION_DAYS`, ver
      [`DATA_RETENTION.md`](DATA_RETENTION.md)); secretos sólo donde deben estar.
- [ ] **Ningún scraping de cadena real activado sin autorización explícita** (política de
      [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md) / [`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md)).
