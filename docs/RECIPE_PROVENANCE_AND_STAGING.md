# Provenance de recetas y promoción staging-first de proveedores

Esta PR (fase 1) hace dos cosas seguras, sin construir todavía la cola/worker/adaptador de ingesta:
**(1)** bloquea las vías heredadas que escribían datos productivos directamente desde proveedores, y
**(2)** registra la procedencia y el estado de verificación de las 100 recetas ya importadas, sin
alterar ninguna cantidad.

## 1. Bloqueo de la promoción directa heredada

El código heredado de Open Prices escribía `Product`, `ProductBarcode`, `ProductPrice`, `DataImport`
con `status=committed` y mappings `is_active=True` **directamente** (sin revisión ni promoción). Eso
queda **bloqueado por defecto** mediante el flag `legacy_direct_provider_writes_enabled` (**False**):

- `open_prices_sync.sync_store` / `sync_all` / `sync_all_and_enrich` y
  `ingredient_matching.map_real_products` lanzan `LegacyProviderWriteBlocked` si el flag está off.
- Los endpoints `POST /api/v1/admin/sources/open-prices/sync` y `/sources/sync-all` devuelven **409**
  con un mensaje claro.
- El flag **nunca** debe activarse por defecto. Los datos productivos vendrán de:
  `staging → revisión → promoción explícita y auditada` (fase 2).

Tests (`tests/api/test_legacy_provider_block.py`) demuestran que una llamada bloqueada **no cambia**
`Product` ni `ProductPrice`. Los tests de la vía heredada siguen cubiertos activando el flag
localmente (`tests/admin/conftest.py`).

## 2. Provenance y verificación de recetas

Las 100 recetas provienen del dataset real **`belenarbizu/recetas-espanolas`** (recetas españolas).
El dataset trae **nombres de ingredientes pero NO cantidades**, así que las cantidades fueron
**estimadas por un LLM** (gpt-4o-mini) a partir de la receta real y sus porciones. Eso queda
registrado, y **una cantidad estimada NUNCA se marca como verificada**.

Columnas nuevas (aditivas, nullable — no se sobrescribe ningún contenido ni cantidad):

- `recipe`: `source_dataset`, `source_reference`, `source_license`, `imported_at`,
  `verification_status` (`pending_review` | `verified` | `rejected`), `estimation_model`,
  `estimation_prompt_version`.
- `recipe_ingredient`: `quantity_source` (`source_original` | `ai_estimated` | `manually_verified`),
  `quantity_confidence`, `verification_status`.

**Backfill idempotente** (`tools.backfill_recipe_provenance`, `--dry-run` / `--apply`): sobre las
recetas `origin=imported` que aún no tienen provenance, fija `source_dataset=belenarbizu/…`,
`imported_at=created_at`, `verification_status=pending_review`, `estimation_model=gpt-4o-mini`,
`estimation_prompt_version=belenarbizu-quantities-v1`, y en sus ingredientes
`quantity_source=ai_estimated`, `verification_status=pending_review`. Fill-only: una segunda
ejecución no cambia nada. **Dry-run de producción** (solo lectura): marcaría **100 recetas** +
**521 líneas de ingrediente** (todas `origin=imported`; ninguna otra receta se toca).

> La migración añade solo columnas; el backfill de datos lo hace la herramienta (dry-run primero),
> nunca la migración.

## 3. Política de uso en el planificador (pendiente de revisión)

Las recetas importadas están `is_public=True` y por tanto el planificador puede usarlas, pero quedan
`verification_status=pending_review`. La metadata ahora **existe** para aplicar una política
explícita y visible (p. ej. distinguir en la interfaz "pendiente de revisión" de "verificada
manualmente", u opcionalmente excluir las pendientes). Esa distinción de interfaz y la política de
uso se abordan en la **fase 2**, junto con el pipeline staging (ProviderSyncJob → ingestion worker →
Open Prices staging-only → candidatos de mapping → endpoints y panel admin) y la **promoción
explícita** a productivo (que exigirá simultáneamente `authorization_status=verified`,
`production_enabled=true`, `production_approved=true`, mapper aprobado, mappings revisados, precio/
moneda/tienda/freshness válidos, envase compatible y aprobación humana con actor + timestamp).
