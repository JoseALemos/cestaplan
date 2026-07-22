# Architecture Decision Records (ADR)

Cada ADR captura una decisión arquitectónica significativa: su contexto, la decisión
tomada, las alternativas consideradas y las consecuencias. Son inmutables: si una
decisión cambia, se crea un ADR nuevo que **supersede** al anterior en lugar de
editarlo.

Formato: [MADR](https://adr.github.io/madr/) simplificado.

| ADR | Título | Estado |
|-----|--------|--------|
| [0001](0001-monorepo-and-stack.md) | Monorepo pnpm+Turborepo (JS) y uv+Python 3.12 (backend) | Aceptado |
| [0002](0002-postgres-job-queue-no-redis.md) | Cola de trabajos sobre PostgreSQL sin Redis en el MVP | Aceptado |
| [0003](0003-decimal-money.md) | Dinero con Decimal/numeric, nunca float | Aceptado |
| [0004](0004-openai-proposes-engine-decides.md) | OpenAI propone; el motor determinista valida y calcula | Aceptado |
| [0005](0005-opaque-session-auth.md) | Sesiones opacas en base de datos, no JWT en el cliente | Aceptado |
| [0006](0006-hybrid-data-sources-no-scraping.md) | Fuentes híbridas sin scraping en el MVP | Aceptado |
| [0007](0007-full-package-cost-model.md) | Coste por envases completos, no por fracción proporcional | Aceptado |
| [0008](0008-price-ingestion-subsystem.md) | Subsistema de ingesta de precios: conectores, cola Postgres, sin scraping de fuentes bloqueadas | Aceptado |

## Plantilla

Copia [`_template.md`](_template.md) para crear un ADR nuevo. Numera de forma
incremental y con cuatro dígitos.
