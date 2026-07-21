# Despliegue — CestaPlan

Cómo ejecutar CestaPlan en local y cómo desplegarlo en Railway. Coherente con las
decisiones canónicas del proyecto y con `docs/SECURITY.md` y `docs/PRIVACY.md`.

Arquitectura de despliegue: cuatro piezas — **web** (Next.js/PWA), **api** (FastAPI),
**worker** (consumidor de cola en PostgreSQL) y **postgres**. Sin Redis, sin K8s, sin
pagos en el MVP. La cola de trabajos vive en Postgres con `SELECT FOR UPDATE SKIP LOCKED`.

---

## 1. Requisitos

- **PostgreSQL 16**.
- **Python 3.12** con [`uv`](https://docs.astral.sh/uv/) para el backend (`api` y
  `worker`).
- **Node.js** con **pnpm** + **Turborepo** para el frontend (`web`).
- Docker + docker-compose son **opcionales**: si Docker no está instalado, usa la ruta
  nativa (sección 2.2).

---

## 2. Desarrollo local

### 2.1 Con docker-compose (recomendado si tienes Docker)

`docker-compose.yml` reproduce el modelo de servicios de Railway (`web`, `api`,
`worker`, `postgres`).

```bash
cp .env.example .env      # rellena los valores necesarios
make up                   # docker compose up -d
```

- `postgres` expone `5432` con healthcheck (`pg_isready`).
- `api` arranca tras Postgres, aplica `alembic upgrade head` y sirve en `:8000`.
- `worker` comparte imagen con `api` y ejecuta `python -m cestaplan_worker.main`.
- `web` sirve el front en `:3000` y apunta a la API vía `NEXT_PUBLIC_API_BASE_URL`.

Parar: `make down`.

### 2.2 Nativo (sin Docker)

Docker puede no estar instalado; el flujo nativo cubre todo el desarrollo. Necesitas un
PostgreSQL accesible (local o remoto) y `DATABASE_URL` apuntando a él.

```bash
make setup      # uv sync (Python) + pnpm install (JS)
make migrate    # alembic upgrade head
make seed       # carga datos demo (opcional)
```

En terminales separadas:

```bash
make api        # uvicorn cestaplan_api.main:app --reload (:8000)
make web        # next dev (:3000)
make worker     # python -m cestaplan_worker.main
```

Utilidades: `make lint`, `make typecheck`, `make test`, `make fmt`.

---

## 3. Modos de despliegue: `self_hosted` vs `cloud`

Controlado por `DEPLOYMENT_MODE` (ver `.env.example`):

| Aspecto             | `self_hosted`                                          | `cloud`                                                       |
|---------------------|--------------------------------------------------------|--------------------------------------------------------------|
| Clave de OpenAI     | El administrador aporta `OPENAI_API_KEY` (`byok`)      | Gestionada por el servidor; **nunca se revela** al cliente   |
| Límites de IA       | Sin cuotas por defecto; puede desactivar la IA         | Registra consumo (`UsageLedger`) y aplica cuotas             |
| Facturación IA      | `AI_BILLING_MODE=byok` o `disabled`                    | `AI_BILLING_MODE=platform` (sin pagos aún en el MVP)         |
| Catálogos           | El admin puede importar catálogos y usar conectores    | Igual, según configuración del operador                      |

En ambos modos, toda función crítica funciona **sin OpenAI**: si `AI_BILLING_MODE=disabled`,
no se contacta con OpenAI y el motor determinista opera con recetas semilla.

---

## 4. Despliegue en Railway

CestaPlan se despliega como **cuatro servicios** dentro de un proyecto Railway, con dos
entornos: **`staging`** y **`production`**. La configuración por servicio está versionada
en [`infra/railway/`](../infra/railway/) (ver su `README.md` para el mapeo detallado).

### 4.1 Servicios

| Servicio   | Root directory  | Config                                  | Dominio          | Comando de arranque                                      |
|------------|-----------------|-----------------------------------------|------------------|----------------------------------------------------------|
| `web`      | `apps/web`      | [`infra/railway/web.json`](../infra/railway/web.json)       | **Público**      | `node apps/web/server.js`                    |
| `api`      | `apps/api`      | [`infra/railway/api.json`](../infra/railway/api.json)       | **Público**      | `uvicorn cestaplan_api.main:app --host 0.0.0.0 --port $PORT` |
| `worker`   | `apps/worker`   | [`infra/railway/worker.json`](../infra/railway/worker.json) | **Sin dominio**  | `python -m cestaplan_worker.main`                        |
| `postgres` | —               | Base de datos gestionada por Railway    | Red privada      | —                                                        |

- **`web`**: público. Sirve la PWA. Healthcheck en `/`.
- **`api`**: público. Healthcheck en **`/health`**. Pre-deploy `alembic upgrade head`
  (sección 4.4).
- **`worker`**: **no** lleva dominio. Consume la cola de generación en Postgres
  (`SELECT FOR UPDATE SKIP LOCKED`, reintentos limitados con backoff y heartbeat).
  Comparte imagen con `api`.
- **`postgres`**: expone `DATABASE_URL` que `api` y `worker` consumen por **red
  privada** (ver 4.3).

### 4.2 Entornos

- **`staging`**: entorno de validación. Datos no productivos. `COOKIE_SECURE=true`.
- **`production`**: entorno real. Secretos propios y distintos de staging (incluido
  `SESSION_SECRET`).
- Cada entorno tiene su propio Postgres y su propio conjunto de variables.

### 4.3 Comunicación privada

- `api` y `worker` acceden a Postgres por la **red privada** de Railway. Referencia
  `DATABASE_URL` como variable de referencia a Postgres (p. ej.
  `${{Postgres.DATABASE_URL}}`), **no** por la URL pública.
- No expongas Postgres a Internet.
- `CORS_ALLOWED_ORIGINS` en `api` se limita al dominio público de `web`.

### 4.4 Migraciones como pre-deploy command

- El servicio `api` define en `api.json` un **`preDeployCommand`: `alembic upgrade head`**.
- Railway ejecuta el *pre-deploy command* **después del build y antes** de arrancar la
  nueva versión, contra la base de datos del entorno. Si la migración falla, el deploy
  se aborta y **no** se promociona la nueva versión (la anterior sigue sirviendo).
- Las migraciones se ejecutan **solo en `api`** (una vez por deploy), no en `worker`,
  para evitar migraciones concurrentes.
- El healthcheck `/health` de `api` retrasa el enrutado de tráfico hasta que la nueva
  versión responde correctamente.

### 4.5 Seguridad del pipeline

- **No autodeploy desde forks no confiables.** Los PRs de forks externos no deben
  desplegar automáticamente ni acceder a secretos de despliegue. El despliegue se
  dispara desde ramas del repositorio de confianza (p. ej. merge a la rama de release),
  con revisión previa.
- Los secretos se inyectan por servicio/entorno en Railway; nunca se versionan (ver
  `docs/SECURITY.md`, sección de gestión de secretos).

---

## 5. Variables de entorno

Fuente única: [`.env.example`](../.env.example). En Railway se definen **por servicio y
por entorno**. Tabla completa:

| Variable                       | Servicios      | Descripción                                                             |
|--------------------------------|----------------|-------------------------------------------------------------------------|
| `DEPLOYMENT_MODE`              | web, api, worker | `self_hosted` \| `cloud`                                              |
| `AI_BILLING_MODE`              | api, worker    | `platform` \| `byok` \| `disabled`                                      |
| `DATABASE_URL`                 | api, worker    | Cadena de conexión a Postgres. En Railway, por **red privada**          |
| `API_HOST`                     | api            | Host de escucha (local `0.0.0.0`). En Railway se usa `$PORT`            |
| `API_PORT`                     | api (local)    | Puerto local del API (`8000`)                                           |
| `API_PUBLIC_URL`              | web, api       | URL pública del API                                                     |
| `WEB_PUBLIC_URL`              | web, api       | URL pública del web                                                     |
| `CORS_ALLOWED_ORIGINS`         | api            | Lista blanca de orígenes CORS (dominio de `web`). Sin comodín           |
| `SESSION_SECRET`               | api, worker    | Secreto de 32+ bytes para material de sesión. Distinto por entorno      |
| `SESSION_TTL_HOURS`            | api            | Vida de la sesión (por defecto `720`)                                   |
| `COOKIE_SECURE`                | api            | `true` en producción (HTTPS)                                            |
| `COOKIE_SAMESITE`              | api            | `lax` \| `strict`                                                       |
| `OPENAI_API_KEY`               | api, worker    | Clave OpenAI. Solo si `AI_BILLING_MODE != disabled`. Nunca en `web`     |
| `OPENAI_MODEL`                 | api, worker    | Modelo OpenAI. No hardcodear en la lógica de negocio                    |
| `OPENAI_REASONING_EFFORT`      | api, worker    | Esfuerzo de razonamiento (p. ej. `medium`)                              |
| `OPENAI_TIMEOUT_SECONDS`       | api, worker    | Timeout de llamada a OpenAI (`60`)                                      |
| `OPENAI_MAX_RETRIES`           | api, worker    | Reintentos de llamada a OpenAI (`2`)                                    |
| `WORKER_POLL_INTERVAL_SECONDS` | worker         | Intervalo de sondeo de la cola (`2`)                                    |
| `WORKER_JOB_MAX_ATTEMPTS`      | worker         | Máximo de intentos por trabajo (`3`)                                    |
| `WORKER_HEARTBEAT_SECONDS`     | worker         | Latido del worker sobre trabajos en curso (`15`)                        |
| `NEXT_PUBLIC_API_BASE_URL`     | web            | URL base del API que consume el front (dominio público de `api`)        |
| `PORT`                         | web, api       | Inyectada por Railway; el `startCommand` la usa                         |

Reglas:

- `OPENAI_API_KEY` y `SESSION_SECRET` **nunca** en el servicio `web` (público).
- `NEXT_PUBLIC_*` es visible en el cliente: solo valores no secretos.
- En producción, `COOKIE_SECURE=true` y `CORS_ALLOWED_ORIGINS` restringido al dominio
  real de `web`.

---

## 6. Comprobaciones post-despliegue

1. `api`: `GET /health` responde `200`.
2. Migraciones aplicadas (el pre-deploy no falló).
3. `web` carga y llega al API (`NEXT_PUBLIC_API_BASE_URL` correcto, CORS OK).
4. `worker` toma trabajos de la cola (logs de heartbeat, sin errores de conexión).
5. Postgres accesible solo por red privada.
