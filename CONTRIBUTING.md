# Contribuir a CestaPlan

Gracias por tu interés en CestaPlan. Este documento explica cómo trabajar en el
proyecto de forma que tus cambios entren rápido y sin fricción.

La prosa del proyecto es en **español**; los identificadores de código
(nombres de variables, tipos, claves) van en **inglés**.

## Antes de empezar

- Lee el [README.md](./README.md) y los principios del proyecto.
- Familiarízate con la documentación relevante en [docs/](./docs/).
- Respeta el [Código de Conducta](./CODE_OF_CONDUCT.md).
- Para dudas abiertas y propuestas, usa **GitHub Discussions**. Para bugs y
  peticiones concretas, abre un *issue* con la plantilla adecuada.

## Principios innegociables (léelos antes de escribir código)

Cualquier PR que incumpla estos principios será rechazado:

- **Nunca inventar precios.** Todo precio lleva **fuente + tienda + fecha**. Si no
  hay dato, se dice; no se sustituye por `0` ni por una estimación disfrazada de
  dato real.
- **No scraping / no datos dudosos.** Nada de *scraping*, ni eludir CAPTCHA o
  medidas anti-bot. Los conectores comunitarios van **desactivados por defecto**.
- **El dinero es exacto.** `Decimal` en Python, `numeric` en Postgres; en JS el
  dinero viaja como **string**. Nunca `float` para dinero.
- **Envases completos.** El coste se calcula comprando envases completos, no
  prorrateando por gramo consumido.
- **Alergias = restricción dura**, decidida por el validador determinista, no por
  el LLM.
- **OpenAI propone; el núcleo determinista valida y calcula.** No muevas lógica de
  seguridad o de cálculo económico al LLM.
- **Privacidad.** Nunca envíes a OpenAI nombres reales, email ni identificadores
  internos; pseudonimiza el contexto.

## Cómo correr el proyecto

Requisitos: **Node 22+**, **pnpm**, **uv + Python 3.12**, **PostgreSQL**
(Docker opcional).

```bash
cp .env.example .env
make setup      # uv sync (apps/api) + pnpm install
make up         # Postgres vía docker compose (o usa tu Postgres local)
make migrate    # alembic upgrade head
make seed       # datos demo sintéticos

# en terminales separadas:
make api        # FastAPI  (:8000)
make web        # Next.js  (:3000)
make worker     # worker de la cola
```

Consulta el [README](./README.md#arranque-rápido) para el detalle completo
(Docker y nativo).

## Dónde vive cada cosa

| Área | Ubicación |
|------|-----------|
| PWA / frontend | `apps/web/` |
| API + motor determinista + OpenAI | `apps/api/` |
| Worker de la cola | `apps/worker/` |
| Contratos (JSON Schema → TS + Zod) | `packages/contracts/` |
| UI compartida | `packages/ui/` |
| Config compartida | `packages/config/` |
| Datos demo / imports / esquemas | `data/demo`, `data/imports`, `data/schemas` |
| Documentación | `docs/` |
| Infra Railway | `infra/railway/` |

Los **contratos** tienen una **fuente única** en `packages/contracts`: se derivan
de los modelos Pydantic v2 y de ahí salen los tipos TS y los esquemas Zod. No
dupliques tipos a mano entre backend y frontend: cambia el contrato.

## Flujo de trabajo

1. **Crea una rama** desde la principal con un nombre descriptivo:
   - `feat/…` nueva funcionalidad · `fix/…` corrección · `docs/…` documentación ·
     `refactor/…` · `test/…` · `chore/…`.
2. **Commits atómicos**: cada commit debe compilar y contar una sola cosa. Mensajes
   claros; se recomienda estilo [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
3. **Mantén el diff pequeño y enfocado.** No mezcles refactors amplios con cambios
   de comportamiento. No arregles cosas "de paso" fuera del alcance del PR.
4. **Actualiza la documentación** afectada en `docs/` y los contratos si cambian.

## Antes de abrir un PR

Ejecuta y deja en verde:

```bash
make lint        # ruff check + pnpm lint (ESLint)
make typecheck   # pyright + pnpm typecheck
make test        # pytest + pnpm test (Vitest/Playwright)
make fmt         # ruff format + prettier
```

### Estilo

- **Python**: **Ruff** (lint + formato) y **Pyright** (tipos). Tipado estricto,
  Pydantic v2 para esquemas.
- **JS/TS**: **ESLint** + **Prettier**, TypeScript en modo estricto, **Zod** para
  validación en la frontera.

## Cómo añadir adaptadores, recetas y fuentes de precios

- **Adaptador de supermercado** → sigue [docs/ADAPTER_GUIDE.md](./docs/ADAPTER_GUIDE.md).
  Todos implementan el contrato único `RetailerAdapter`. Un conector comunitario
  nuevo va **desactivado por defecto** y **sin scraping**. Para proponer uno antes
  de implementarlo, abre un issue con la plantilla **new_adapter**.
- **Recetas** → sigue [docs/RECIPES_GUIDE.md](./docs/RECIPES_GUIDE.md) (esquema,
  unidades canónicas, alérgenos, sustituciones, validación, `is_synthetic`).
- **Fuentes de precios** → sigue [docs/PRICE_SOURCES_GUIDE.md](./docs/PRICE_SOURCES_GUIDE.md)
  (formato CSV/JSON de importación, `source_type` permitidos, reglas y licencias).

## Buenas primeras contribuciones

Busca issues etiquetados **`good first issue`**: están acotados y documentados para
empezar sin conocer todo el proyecto. Los **`help wanted`** son un buen segundo
paso. Si no sabes por dónde empezar, pregunta en **GitHub Discussions**.

## Checklist del PR

La plantilla de PR incluye la lista completa. En resumen: lint, typecheck y tests
en verde; documentación actualizada; sin *scraping*; sin precios inventados;
`Decimal`/`numeric`/string para dinero.

## Licencia de tus aportaciones

Al contribuir, aceptas que tu **código** se publique bajo la licencia
[MIT](./LICENSE). Si aportas **datos**, indica su procedencia y licencia (ver
[docs/DATA_SOURCES.md](./docs/DATA_SOURCES.md)); no subas catálogos comerciales
que no puedas redistribuir.
