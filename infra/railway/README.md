# Configuración de Railway por servicio

Esta carpeta contiene un archivo de configuración de Railway (schema
`railway.json`/`railway.toml`) por cada servicio de despliegue. CestaPlan es un
monorepo, así que **cada servicio Railway se distingue por su archivo de
configuración** (`infra/railway/<servicio>.json`), que selecciona el `Dockerfile` y el
`startCommand`.

> **Contexto de build = raíz del repositorio.** Los `Dockerfile` esperan que el
> contexto de build sea la **raíz del repo** (igual que `docker-compose.yml`:
> `context: .`, `dockerfile: apps/{api,web}/Dockerfile`). El `Dockerfile` de `web`
> necesita `pnpm-lock.yaml` / `pnpm-workspace.yaml` de la raíz, y el de `api` copia
> `apps/api/...` relativo a la raíz. Por eso, en Railway, **deja el *Root Directory*
> del servicio en la raíz del repo** (vacío / `/`) y diferencia cada servicio por su
> *Railway config file*. Fijar el *Root Directory* a `apps/api` o `apps/web` rompería
> los `COPY` del Dockerfile.

Ver la guía completa en [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md).

## Mapa de servicios

| Servicio Railway | Archivo de config    | Root directory | Dockerfile             | Dominio público | Notas                                                  |
|------------------|----------------------|:--------------:|------------------------|:---------------:|--------------------------------------------------------|
| `web`            | [`web.json`](web.json)       | `/` (raíz) | `apps/web/Dockerfile`  | Sí             | Next.js (PWA) standalone. Healthcheck `/`. `node apps/web/server.js` |
| `api`            | [`api.json`](api.json)       | `/` (raíz) | `apps/api/Dockerfile`  | Sí             | FastAPI. Pre-deploy `alembic upgrade head`, health `/health` |
| `worker`         | [`worker.json`](worker.json) | `/` (raíz) | `apps/api/Dockerfile`  | **No**         | Consume la cola en Postgres. **Sin dominio, sin healthcheck** |
| `postgres`       | —                    | —              | —                      | No (red privada)| Base de datos gestionada por Railway. No lleva config aquí |

> **El `worker` reutiliza la imagen del `api`.** Ambos servicios construyen el mismo
> `apps/api/Dockerfile`; solo cambia el `startCommand` (`worker.json` lo fija a
> `python -m cestaplan_worker.main`). El código del worker vive en
> `apps/api/src/cestaplan_worker`, por eso el `Dockerfile` es compartido. En el
> servicio `worker` de Railway, apunta su *Railway config file* a
> `infra/railway/worker.json` (o, si prefieres configurarlo en el dashboard, usa el
> `apps/api/Dockerfile` con *start command* `python -m cestaplan_worker.main`). El
> directorio `apps/worker/` es solo un marcador del monorepo; no contiene el Dockerfile.

## Cómo asociar cada archivo a su servicio

En Railway, la configuración por archivo se enlaza con el servicio de dos formas
equivalentes; usa la que prefieras:

1. **Config as code (recomendado).** En cada servicio, *Settings → Config-as-code →
   Railway config file*, indica la ruta relativa al repo:
   - servicio `web`  → `infra/railway/web.json`
   - servicio `api`  → `infra/railway/api.json`
   - servicio `worker` → `infra/railway/worker.json`
2. **Dashboard.** Alternativamente, fija en la UI el `Dockerfile Path` y el
   `Start Command` equivalentes a los del JSON. En ambos casos, **deja el *Root
   Directory* en la raíz del repo** (ver la nota de contexto de build arriba).

Pasos por servicio:

1. Crea el servicio en el proyecto y entorno (`staging` o `production`).
2. Conecta el repositorio y **deja el *Root Directory* en la raíz** (vacío / `/`).
3. Apunta el **Railway config file** al JSON de esta carpeta.
4. Configura las **variables de entorno** del servicio (siguiente sección).
5. Para `api`, verifica que el *pre-deploy command* (`alembic upgrade head`) y el
   *healthcheck* (`/health`) quedan activos según `api.json`. Las migraciones se
   ejecutan **solo** en `api` (una vez por deploy), nunca en `worker`.
6. Para `worker`, **no** añadas dominio público ni healthcheck. Su config file es
   `infra/railway/worker.json` (mismo Dockerfile que `api`, distinto `startCommand`).

## Entornos y despliegue

- Dos entornos por proyecto: **`staging`** (datos no productivos) y **`production`**
  (secretos propios, distintos de staging, incluido `SESSION_SECRET`). Cada entorno
  tiene su **propio Postgres** y su propio conjunto de variables.
- **Red privada.** `api`, `worker` y `postgres` se comunican por la **red privada** de
  Railway. Referencia `DATABASE_URL` como variable de referencia a Postgres
  (`${{Postgres.DATABASE_URL}}`), nunca por la URL pública. Solo `web` y `api` exponen
  dominio público; `worker` y `postgres` no.
- **Sin autodeploy desde forks no confiables.** No conectes el despliegue automático a
  PRs de forks externos ni les des acceso a los secretos del proyecto. Dispara los
  deploys desde ramas del repositorio de confianza (merge revisado a la rama de
  release). El CI (`.github/workflows/ci.yml`) corre sin secretos y no despliega.

## Variables de entorno por servicio

Las variables se definen **por servicio y por entorno** en Railway (no en estos
archivos). La lista completa y su significado está en `.env.example` y en
`docs/DEPLOYMENT.md`. Resumen:

| Variable                    | web | api | worker | Origen / notas                                             |
|-----------------------------|:---:|:---:|:------:|------------------------------------------------------------|
| `DATABASE_URL`              |     | ✓   | ✓      | Referencia a Postgres por **red privada** (`${{Postgres.DATABASE_URL}}`) |
| `DEPLOYMENT_MODE`           | ✓   | ✓   | ✓      | `self_hosted` \| `cloud`                                   |
| `AI_BILLING_MODE`           |     | ✓   | ✓      | `platform` \| `byok` \| `disabled`                         |
| `SESSION_SECRET`            |     | ✓   | ✓      | Secreto de 32+ bytes, por entorno                          |
| `SESSION_TTL_HOURS`         |     | ✓   |        | Por defecto `720`                                          |
| `COOKIE_SECURE`             |     | ✓   |        | `true` en producción                                       |
| `COOKIE_SAMESITE`           |     | ✓   |        | `lax` \| `strict`                                          |
| `CORS_ALLOWED_ORIGINS`      |     | ✓   |        | Lista blanca (dominio público de `web`)                    |
| `API_PUBLIC_URL`            | ✓   | ✓   |        | URL pública de `api`                                       |
| `WEB_PUBLIC_URL`            | ✓   | ✓   |        | URL pública de `web`                                       |
| `NEXT_PUBLIC_API_BASE_URL`  | ✓   |     |        | Debe apuntar al dominio público de `api`                   |
| `OPENAI_API_KEY`            |     | ✓   | ✓      | Solo si `AI_BILLING_MODE != disabled`. Nunca en `web`      |
| `OPENAI_MODEL`              |     | ✓   | ✓      | No hardcodear en código                                    |
| `OPENAI_REASONING_EFFORT`   |     | ✓   | ✓      |                                                            |
| `OPENAI_TIMEOUT_SECONDS`    |     | ✓   | ✓      |                                                            |
| `OPENAI_MAX_RETRIES`        |     | ✓   | ✓      |                                                            |
| `WORKER_POLL_INTERVAL_SECONDS` |  |     | ✓      |                                                            |
| `WORKER_JOB_MAX_ATTEMPTS`   |     |     | ✓      |                                                            |
| `WORKER_HEARTBEAT_SECONDS`  |     |     | ✓      |                                                            |
| `PORT`                      | ✓   | ✓   |        | Inyectada por Railway; el `startCommand` la usa            |

Notas:

- **Nunca** pongas `OPENAI_API_KEY` ni `SESSION_SECRET` en el servicio `web` (es
  público y de cara al cliente).
- Referencia `DATABASE_URL` mediante variable de referencia a Postgres para forzar
  **comunicación por red privada** (no expongas el Postgres a Internet).
- Mantén valores distintos de `SESSION_SECRET` entre `staging` y `production`.
