# Preparación del catálogo y por qué un plan sale "inviable"

Explica cómo el planificador decide que **no** puede generar un plan, cómo se diagnostica la causa
exacta (sin atribuirla falsamente al presupuesto), y qué falta para disponer de recetas y precios
reales — todo sin `seed_demo`, sin inventar datos y manteniendo las fuentes externas fuera del
planificador hasta superar los gates.

## 1. La ejecución fallida analizada

La última generación real terminó `failed`. El `infeasibility_report` real (saneado) fue:

```
status: infeasible
min_budget_found: null            ← el presupuesto NUNCA se evaluó
minimal_conflict: ["no_candidate_for:breakfast", "no_candidate_for:lunch", "no_candidate_for:dinner"]
offending_products: []
budget: 150 €  (amplio)
```

**Causa exacta:** el solver recibió **cero recetas candidatas** para cada comida. No es un problema
de presupuesto (150 € era holgado y `min_budget_found` es `null`), ni de tienda. La base productiva
está **vacía**: `Recipe = 0`, ingredientes 0, productos 0, precios 0, staging 0, `CrawlRun = 0`. Es
decir: **`no_active_recipes`**.

El fallback genérico ("con las restricciones y el presupuesto actuales…") aparecía porque el motor
solo distinguía dos fallos (slot sin candidato / plan por encima del presupuesto) y devolvía acciones
crudas (`add_recipes`, `change_store`, `reduce_meals`) que insinúan tienda/presupuesto.

## 2. Preflight determinista (nuevo)

Antes de lanzar el optimizador, `services/planner_preflight.py` comprueba la precondición y, si es
imposible, **no ejecuta el solver** y devuelve una causa **tipada**. Orden: recetas → catálogo de la
cadena → productos mapeados → precios → recetas costeables → variedad. **El presupuesto no se evalúa
aquí**, así que el mensaje de presupuesto nunca puede aparecer para un catálogo vacío.

Códigos (`PreflightCode`): `no_active_recipes`, `no_compatible_recipes`, `no_retailer_selected`,
`retailer_without_catalog`, `no_mapped_products`, `no_product_prices`, `no_costable_recipes`,
`insufficient_recipe_variety`, `genuine_budget_infeasibility` (solo el motor), `hard_constraints_infeasible`,
`optimizer_error`. Solo `genuine_budget_infeasibility` recomienda subir el presupuesto y expone
`minimum_budget` (calculado de verdad por el motor).

La UI traduce las acciones tipadas (`ActionCode`) — nunca muestra el slug ni usa `replace("_"," ")` —
y solo permite **Reintentar** cuando la precondición determinista puede haber cambiado
(`genuine_budget_infeasibility`, `optimizer_error`).

**Sin cadena no hay plan.** El preflight exige cadena: si `retailer_id=None`, se detiene en
`no_retailer_selected` **antes** de construir el catálogo o consultar precios — no se mezclan precios
de otras cadenas (un plan siempre se calcula contra una única cadena; `_latest_prices(None)` devuelve
un catálogo vacío). Orden exacto: `no_active_recipes → no_retailer_selected → retailer_without_catalog →
no_mapped_products → no_product_prices → no_costable_recipes → insufficient_recipe_variety → ok`.

**`no_compatible_recipes` NO es del preflight.** Corresponde al **filtrado real de candidatos** del
motor (alérgenos/dieta/equipamiento/tipo de comida) y lo emite el enriquecimiento de la infeasibility
del motor (`plan_service._enrich_infeasibility`). El preflight nunca aproxima la compatibilidad
contando recetas públicas.

## 3. Panel de preparación (admin)

`GET /api/v1/admin/planner-readiness` (`services/catalog_readiness.py`) resume: recetas activas /
costeables, ingredientes, mapeos aprobados, productos staging / productivos, precios productivos,
cadenas disponibles, última sincronización y bloqueadores. Estado global:
`Sin recetas → Sin catálogo → Pendiente de mapeos → Solo staging → Sin precios → Preparado para
revisión → Disponible`. **Nunca "Disponible"** salvo que algún proveedor tenga
`production_enabled` **y** `production_approved` (staging/shadow nunca cuentan como producción).

## 4. Qué falta para datos reales

1. **Recetas.** No hay ninguna (`Recipe = 0`). El planificador necesita recetas públicas con
   ingredientes. Deben añadirse/importarse (no `seed_demo`, no inventar).
2. **Precios.** Requieren una sincronización real de proveedor hacia staging y, tras superar los
   gates (mapper/calidad/cobertura/aprobación humana), promoción a `ProductPrice`.

## 5. Estado de ejecución de los proveedores en cloud

- Servicios Railway: `api`, `web`, `Postgres`, `worker` (worker de planes: `python -m cestaplan_worker.main`).
  **No existe** servicio de ingesta (`ingestion-scheduler`/`ingestion-worker`).
- **Todas** las variables de proveedor están **ausentes**: `PRICE_PROVIDERS_ENABLED` (→ desactivado
  por defecto), `PARSE_BOT_API_KEY`, `PARSE_BOT_*_BASE_URL`, `APIFY_API_TOKEN`, `APIFY_MERCADONA_*`,
  `OPEN_PRICES_*`.
- Conclusión: **Parse.bot, Apify y Open Prices están desactivados, sin credenciales ni URL, y nunca
  se han invocado** (`CrawlRun = 0`). Los 9 `ProviderActivation` solo tienen los derechos
  bootstrapped; ninguno con producción.

## 6. Primera sincronización controlada de Alcampo — bloqueada

Las precondiciones de `§8` no se cumplen en cloud: **clave ausente** (`PARSE_BOT_API_KEY`),
**URL ausente** (`PARSE_BOT_ALCAMPO_BASE_URL`), **proveedores desactivados** (`PRICE_PROVIDERS_ENABLED`
ausente) y **sin worker de ingesta**. No se puede ejecutar un run real sin inventar credenciales
(prohibido). Para desbloquearlo, el titular debe: (a) configurar en el servicio de ingesta
`PRICE_PROVIDERS_ENABLED=true`, `PARSE_BOT_API_KEY`, `PARSE_BOT_ALCAMPO_BASE_URL`,
`PARSE_BOT_ALCAMPO_ENABLED=true`; (b) desplegar el worker de ingesta. Entonces se habilita **solo**
transporte/captura/normalización/staging/shadow (nunca `production_enabled`/`production_approved`) y se
lanza un run acotado (1 página, ≤10 productos, sin promover a `ProductPrice`).
