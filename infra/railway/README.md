# Configuración de Railway por servicio

Esta carpeta contiene un archivo de configuración de Railway (schema
`railway.json`/`railway.toml`) por cada servicio de despliegue. CestaPlan es un
monorepo, así que **cada servicio Railway apunta a un *root directory* distinto** dentro
del mismo repositorio y usa su propio archivo de configuración.

Ver la guía completa en [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md).

## Mapa de servicios

| Servicio Railway | Archivo de config    | Root directory  | Dominio público | Notas                                                  |
|------------------|----------------------|-----------------|:---------------:|--------------------------------------------------------|
| `web`            | [`web.json`](web.json)       | `apps/web`  | Sí             | Next.js (PWA). Healthcheck `/`                          |
| `api`            | [`api.json`](api.json)       | `apps/api`  | Sí             | FastAPI. Pre-deploy `alembic upgrade head`, health `/health` |
| `worker`         | [`worker.json`](worker.json) | `apps/worker` | **No**       | Consume la cola en Postgres. **Sin dominio**           |
| `postgres`       | —                    | —               | No (red privada)| Base de datos gestionada por Railway. No lleva config aquí |

> `worker` comparte imagen con `api` (mismo `apps/api/Dockerfile`) pero cambia el
> `startCommand`. Su *root directory* de servicio es `apps/worker`; ajústalo si tu
> layout de despliegue difiere.

## Cómo asociar cada archivo a su servicio

En Railway, la configuración por archivo se enlaza con el servicio de dos formas
equivalentes; usa la que prefieras:

1. **Config as code (recomendado).** En cada servicio, *Settings → Config-as-code →
   Railway config file*, indica la ruta relativa al repo:
   - servicio `web`  → `infra/railway/web.json`
   - servicio `api`  → `infra/railway/api.json`
   - servicio `worker` → `infra/railway/worker.json`
2. **Root directory + `railway.json` local.** Alternativamente, fija el *Root Directory*
   del servicio (`apps/web`, `apps/api`, `apps/worker`) y coloca/enlaza el archivo de
   config correspondiente. Los campos definidos aquí sobrescriben los de la UI.

Pasos por servicio:

1. Crea el servicio en el proyecto y entorno (`staging` o `production`).
2. Conecta el repositorio y fija el **Root Directory** del servicio.
3. Apunta el **Railway config file** al JSON de esta carpeta.
4. Configura las **variables de entorno** del servicio (siguiente sección).
5. Para `api`, verifica que el *pre-deploy command* (`alembic upgrade head`) y el
   *healthcheck* (`/health`) quedan activos según `api.json`.
6. Para `worker`, **no** añadas dominio público.

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
