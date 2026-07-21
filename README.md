# CestaPlan

**Del presupuesto al plato, sin sorpresas en la caja.**

CestaPlan es una PWA de código abierto que planifica tus comidas partiendo de una
restricción real: **cuánto quieres gastar**. Le dices dónde compras, cuánto puedes
gastar, para cuántas personas y qué comidas necesitas; CestaPlan genera recetas,
calcula los **envases completos** que hay que comprar y prepara una lista de la
compra adaptada a una tienda concreta.

<!-- Badges placeholder — sustituir por los reales cuando exista CI/publicación -->
[![CI](https://img.shields.io/badge/CI-pending-lightgrey.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
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
- Un **motor determinista** que valida restricciones, calcula envases y coste, y
  produce planes **reproducibles y auditables**.
- Una **PWA mobile-first** con lista de la compra que funciona **offline**
  (estado local en IndexedDB).
- **Self-hostable**: todas las funciones críticas operan **sin OpenAI**.

## Qué NO es CestaPlan

- **No** es un comparador de precios listo para usar. No trae catálogos comerciales
  cargados; tú aportas los datos (importación CSV/JSON, entrada manual, conectores
  comunitarios opcionales, datasets abiertos).
- **No** hace *scraping* ni elude CAPTCHA/anti-bot en el MVP.
- **No** da consejo médico ni nutricional profesional. Es una herramienta de
  planificación con información **orientativa**.
- El **LLM no decide seguridad de alergias ni cálculos económicos**: solo propone;
  el núcleo determinista valida y calcula.

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
7. **Datos honestos.** No *scraping* en el MVP; conectores comunitarios
   desactivados por defecto; nunca presentar estimaciones como datos reales.
8. **Privacidad primero.** A OpenAI nunca se envían nombres reales, email ni
   identificadores internos: el contexto se pseudonimiza.

## Pantallas (capturas conceptuales)

Descripción textual del recorrido principal (mobile-first):

1. **Onboarding / hogar** — crear cuenta, definir el hogar y sus miembros, número
   de personas, roles (owner, editor, viewer).
2. **Perfil dietético y alergias** — alergias (restricción dura), restricciones
   dietéticas, preferencias y equipamiento de cocina disponible.
3. **Selección de tienda** — cadena + provincia/localidad + código postal + tienda
   concreta, con fecha de actualización del catálogo y cobertura de precios visible.
4. **Definición de comidas** — cuántos desayunos, comidas, meriendas y cenas, para
   cuántas raciones, con huecos permitidos, tuppers para el trabajo, tiempo máximo
   de preparación, repeticiones y cocinar-para-varios-días.
5. **Presupuesto** — importe objetivo como restricción.
6. **Generación del plan** — pantalla de progreso asíncrono (queued →
   collecting_data → generating_candidates → validating → optimizing → completed).
7. **Plan resultante** — recetas propuestas, coste total con **cobertura de
   precios** (coste conocido vs. coste estimado, rango cuando falten datos).
8. **Detalle de receta** — ingredientes, pasos, alérgenos, sustituciones,
   reutilización de sobras; regenerar una comida concreta; marcar
   favorito/rechazado.
9. **Lista de la compra** — agrupada por categorías, por envases completos, con
   coste imputado, **funcionamiento offline** (IndexedDB) y marcado de comprado.
10. **Explicación / auditoría** — por qué se eligió cada plato y, si no hay solución
    posible, el **conjunto mínimo de restricciones en conflicto** y cómo relajarlas.

## Stack

| Capa | Tecnología |
|------|-----------|
| Frontend | Next.js (App Router) + React + TypeScript estricto + Tailwind + TanStack Query + React Hook Form + Zod + PWA (service worker) + IndexedDB |
| Backend | Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic + HTTPX + SDK oficial de OpenAI |
| Base de datos | PostgreSQL (cola de trabajos con `SELECT FOR UPDATE SKIP LOCKED`, sin Redis) |
| Contratos | Fuente única en `packages/contracts`: JSON Schema desde Pydantic v2 → tipos TS + esquemas Zod |
| Calidad | Ruff + Pyright (Python) · ESLint + Prettier + Vitest + Playwright (JS) · Pytest |
| Infra | Docker + docker-compose + GitHub Actions + Railway |
| Monorepo | pnpm workspaces + Turborepo (JS) · uv + Python 3.12 (backend) |

## Estructura del monorepo

```
cestaplan/
├── apps/
│   ├── web/            # PWA Next.js (App Router)
│   ├── api/            # FastAPI + motor determinista + integración OpenAI
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
├── LICENSE · .env.example
└── README.md
```

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
make seed          # carga datos demo (retailer + recetas sintéticos)

# en terminales separadas:
make api           # FastAPI en :8000
make web           # Next.js en :3000
make worker        # worker de la cola
```

Targets útiles del `Makefile`: `make lint`, `make typecheck`, `make test`,
`make fmt`. Ejecuta `make help` para ver todos.

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
| [docs/CONTRIBUTING.md](./docs/CONTRIBUTING.md) | Contribución (remite a la guía raíz) |
| [docs/ADAPTER_GUIDE.md](./docs/ADAPTER_GUIDE.md) | Guía de adaptadores de supermercado |
| [docs/RECIPES_GUIDE.md](./docs/RECIPES_GUIDE.md) | Guía para contribuir recetas |
| [docs/PRICE_SOURCES_GUIDE.md](./docs/PRICE_SOURCES_GUIDE.md) | Guía de fuentes de precios |
| [docs/ROADMAP.md](./docs/ROADMAP.md) | Hoja de ruta por fases |
| [docs/adr/](./docs/adr/) | Architecture Decision Records |

Comunidad: [CONTRIBUTING.md](./CONTRIBUTING.md) ·
[CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) · [SECURITY.md](./SECURITY.md) ·
GitHub Discussions.

## Licencia

El **código fuente** de CestaPlan está bajo licencia [MIT](./LICENSE).

Los **datos** (catálogos de producto, extractos de Open Food Facts, datos demo,
etc.) se rigen por **sus propias licencias**, documentadas con su procedencia en
[docs/DATA_SOURCES.md](./docs/DATA_SOURCES.md). En particular, **Open Food Facts**
se distribuye bajo **ODbL** (requiere atribución y *share-alike* de la base de
datos). No asumas que un catálogo comercial se puede redistribuir bajo la MIT.

## Aviso sanitario

> CestaPlan facilita la planificación y ofrece información orientativa. No sustituye
> el consejo de un profesional sanitario. Comprueba siempre las etiquetas de los
> productos en caso de alergia o intolerancia.
