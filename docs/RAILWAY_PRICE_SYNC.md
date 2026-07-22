# Sincronización de precios en Railway

Cómo se despliegan y operan el **scheduler-cron** y el **worker** de la ingesta de
precios en Railway, con su equivalente autohospedado (docker-compose / nativo),
horario UTC, idempotencia, ejecuciones manuales y recuperación de fallos.

Configuración: [`infra/railway/ingestion-scheduler.json`](../infra/railway/ingestion-scheduler.json),
[`infra/railway/ingestion-worker.json`](../infra/railway/ingestion-worker.json),
[`infra/railway/README.md`](../infra/railway/README.md). Ver también:
[`DEPLOYMENT.md`](DEPLOYMENT.md), [`PRICE_INGESTION.md`](PRICE_INGESTION.md),
[`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md).

---

## 1. Dos servicios nuevos

La ingesta de precios añade **dos** servicios Railway, independientes del `worker` de
planes existente (que consume la cola `cestaplan_worker.main`) y del cron
`open-prices-sync`:

| Servicio Railway | Config | Comando | Tipo | Dominio | Restart |
|------------------|--------|---------|------|:-------:|---------|
| `ingestion-scheduler` | `ingestion-scheduler.json` | `python -m cestaplan_api.jobs.schedule_daily_price_sync` | **Cron diario** | **No** | `NEVER` |
| `ingestion-worker` | `ingestion-worker.json` | `python -m cestaplan_api.jobs.crawl_worker` | Demonio | **No** | `ON_FAILURE` (máx. 10) |

Ambos **reutilizan la imagen del `api`** (`apps/api/Dockerfile`); sólo cambia el
`startCommand`. Ninguno lleva dominio público ni healthcheck: no atienden HTTP.
Comparten la red privada y el mismo `DATABASE_URL` que `api` y `worker`.

> **¿Playwright?** No es necesario para los conectores activos (`Demo`, `Open
> Prices`). Sólo haría falta un navegador Playwright en la imagen **si y cuando** un
> conector concreto lo requiriese para renderizar contexto legítimo (ver el orden de
> preferencia de fuentes en [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md#3-orden-de-preferencia-de-fuentes)).
> Mientras tanto, la imagen del `api` basta.

---

## 2. `ingestion-scheduler` — cron diario

Servicio **cron** (no demonio): Railway lo arranca según `cronSchedule`, ejecuta el
comando, crea los `CrawlRun` + `CrawlJob` del día y termina (`restartPolicyType:
NEVER`).

```json
{
  "deploy": {
    "startCommand": "python -m cestaplan_api.jobs.schedule_daily_price_sync",
    "cronSchedule": "0 3 * * *",
    "restartPolicyType": "NEVER"
  }
}
```

- **Horario:** `0 3 * * *` = **03:00 UTC** cada día. Railway interpreta el cron en
  **UTC**. Ajusta la hora si prefieres otra franja (p. ej. valle nocturno de la
  fuente); el subsistema no depende de una hora concreta.
- **Idempotente por diseño** (`ingestion/scheduler.py`): advisory lock de Postgres +
  freshness por `(retailer, store, run_type)` con cadencia en días. Si el cron se
  dispara dos veces, o si se solapa con una ejecución manual, **no** se duplican jobs.
- No programa retailers cuyo conector esté `disabled`/`unsupported`/`permission_required`
  o con el circuito abierto.

---

## 3. `ingestion-worker` — demonio de la cola

Consume la cola `CrawlJob` con `SELECT ... FOR UPDATE SKIP LOCKED`, procesa un job a
la vez y actualiza `ConnectorState`.

```json
{
  "deploy": {
    "startCommand": "python -m cestaplan_api.jobs.crawl_worker",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

- `restartPolicyType: ON_FAILURE` para que Railway lo reinicie si el proceso muere.
  Al arrancar, `recover_stuck_jobs()` re-encola los jobs abandonados por la instancia
  anterior (heartbeat obsoleto), sin consumir intento.
- Puede escalarse a varias réplicas: el `SKIP LOCKED` garantiza que dos workers nunca
  tomen el mismo job, y `domain_limits` evita que un retailer monopolice la flota.
- Apagado limpio con SIGTERM (`StopFlag`): termina el job en curso y para.

---

## 4. Ejecuciones manuales

Desde cualquier entorno con acceso a la base de datos (una shell del servicio `api` o
local con el `DATABASE_URL` correcto):

```bash
# Forzar la programación de un retailer ahora (ignora freshness)
python -m cestaplan_api.jobs.sync_retailer --retailer demofixturemart

# Forzar una tienda concreta (por public_id UUID)
python -m cestaplan_api.jobs.sync_store --store-id <uuid>

# Ver salud de conectores (estado, fallos, circuito)
python -m cestaplan_api.jobs.connector_health

# Re-encolar los jobs fallidos / dead-letter de un run
python -m cestaplan_api.jobs.retry_failed --run-id <uuid>

# Re-parsear una captura almacenada
python -m cestaplan_api.jobs.reprocess_capture --capture-id <uuid>
```

Una ejecución manual del scheduler es segura frente al cron: comparten advisory lock e
idempotencia.

---

## 5. Equivalente autohospedado

El subsistema no depende de Railway. En autohospedaje, los dos servicios son procesos
del mismo paquete.

### 5.1 Nativo (systemd / supervisor)

```bash
# Worker (demonio)
python -m cestaplan_api.jobs.crawl_worker

# Scheduler (vía cron del sistema, p. ej. crontab a las 03:00 UTC)
0 3 * * *  cd /app/apps/api && python -m cestaplan_api.jobs.schedule_daily_price_sync
```

### 5.2 docker-compose

Reutilizan la imagen del `api` (igual que en Railway), cambiando el `command`:

```yaml
services:
  ingestion-worker:
    image: cestaplan-api          # misma imagen que el servicio api
    command: python -m cestaplan_api.jobs.crawl_worker
    environment:
      DATABASE_URL: ${DATABASE_URL}
    restart: on-failure

  # El scheduler se lanza por el cron del host (o un sidecar tipo ofelia),
  # ejecutando una vez al día:
  #   docker compose run --rm ingestion-worker \
  #     python -m cestaplan_api.jobs.schedule_daily_price_sync
```

El `docker-compose.yml` del repo ya define `api`, `web`, `worker` y `postgres`;
`ingestion-worker` sigue el mismo patrón que `worker`, cambiando el comando.

---

## 6. Recuperación de fallos

| Situación | Comportamiento del sistema | Acción del operador |
|-----------|----------------------------|---------------------|
| El worker muere a mitad de un job | El job queda `locked` con heartbeat obsoleto | Railway reinicia el servicio (`ON_FAILURE`); al arrancar, `recover_stuck_jobs()` lo re-encola sin consumir intento. |
| Un job falla de forma transitoria | Backoff exponencial + jitter, hasta `max_attempts` | Ninguna; se reintenta solo. |
| Un job agota reintentos | Pasa a `dead_letter` | `retry_failed --run-id <uuid>` tras corregir la causa. |
| El cron se dispara dos veces | Advisory lock + idempotencia evitan duplicados | Ninguna. |
| Un conector falla repetidamente | Circuito abierto; `ConnectorState = temporarily_blocked` | Investigar (`connector_health`); el scheduler lo omite hasta que expire el enfriado. |

Runbook completo de incidentes en
[`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md).

---

## 7. Variables de entorno de estos servicios

Ambos necesitan `DATABASE_URL` (referencia a Postgres por **red privada**,
`${{Postgres.DATABASE_URL}}`). El comportamiento de la ingesta se afina con las
variables `SCRAPING_*`, `RAW_CAPTURE_RETENTION_DAYS`, `STALE_PRICE_HOURS`,
`EXPIRED_PRICE_HOURS`, `CONNECTOR_FAILURE_THRESHOLD`, `CONNECTOR_CIRCUIT_OPEN_MINUTES`
y los flags `*_CONNECTOR_ENABLED` (ver
[`PRICE_INGESTION.md`](PRICE_INGESTION.md#4-variables-de-entorno)). **Nunca** pongas
secretos en un servicio con dominio público; estos dos no lo tienen.
