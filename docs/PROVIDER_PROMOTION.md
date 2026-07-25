# Promoción staging → producción de un proveedor (fase 2)

La fase 1 bloqueó las vías heredadas que escribían `Product` / `ProductPrice` / mappings activos
directamente desde un proveedor (flag `legacy_direct_provider_writes_enabled=False`). Esta fase 2
construye la **única vía sancionada** para que datos de un proveedor lleguen a las tablas
productivas que lee el motor de planes: una **promoción explícita, auditada y aprobada por un
humano**.

El pipeline de captura ya existía y **no** se reconstruye: adaptador Open Prices +
`run_provider_sync(mode=STAGING)` escriben observaciones `staging_only=True`; `targeted_discovery`
genera candidatos `ProviderIngredientMapping (mapping_status=candidate)`; el panel de **Mapeos**
(`/api/v1/admin/ingredient-product-mappings`) permite aprobar/rechazar. Nada de eso cuesta un plan
real. Lo que faltaba —y añade esta fase— es el **puente** desde ahí a producción.

## El puente: `services/provider_promotion.py`

Dos operaciones, ambas idempotentes y ambas que **no escriben nada** si el gate no está limpio:

### 1. `approve_provider_production(db, *, provider_code, actor_id)`
La **acción humana de aprobación de producción**. Es el único sitio donde se ponen a `True`
`production_enabled` / `production_approved`, y registra `production_approved_by` (FK a `user`) +
`production_approved_at`. Se **niega** (`PromotionBlocked`, HTTP 409) salvo que se cumplan TODOS los
prerequisitos no-humanos: proveedores habilitados y sin kill-switch, `transport_status=operational`,
`mapper_status=verified`, `data_quality_status=accepted` y (si `provider_require_rights_approval`)
`data_rights_status` compatible. Idempotente: reaprobar conserva el aprobador + timestamp original
(la auditoría no se sobrescribe).

### 2. `promote_provider_to_production(db, *, provider_code, actor_id, dry_run=False)`
Materializa lo aprobado. Se **niega** salvo que el proveedor pase `evaluate_production` **y** tenga
`production_enabled AND production_approved`. Entonces:

1. Por cada candidato aprobado (`is_selectable_for_costing` + `normalized_product_id` presente)
   crea —si no existe ya— un `IngredientProductMapping(is_active=True, verification_status=
   human_verified, verified_by=actor)` para `(ingredient, producto canónico)`. El retailer se
   resuelve desde el `retailer_slug` autoritativo del candidato (un plan se cuesta contra **una**
   cadena).
2. Promueve las observaciones `staging_only=True` de esa cadena a `staging_only=False`.
3. Reutiliza `CurrentPriceService.project_current_prices(retailer_id)` (ya probado) para escribir
   `ProductPrice` desde la observación válida más reciente por variante.

**Nunca fabrica** un precio, producto o mapping: un candidato sin producto canónico se salta; una
observación sin tienda no se proyecta. Idempotente: un mapping ya presente no se duplica y una
observación ya promovida no se recuenta. El servicio hace `flush` pero **no** `commit` — el llamante
posee la transacción, así que un `dry_run` es "ejecútalo y haz rollback" con conteos exactos.

## Endpoints admin (`routers/provider_promotion_admin.py`)

Solo admin de plataforma; mutaciones con CSRF. Un gate bloqueado devuelve **409** con razones
tipadas, nunca una escritura.

- `GET  /api/v1/admin/providers/{code}/promotion-status` — razones del gate + conteos (solo lectura).
- `POST /api/v1/admin/providers/{code}/production-approval` — aprobación humana (actor + timestamp).
- `POST /api/v1/admin/providers/{code}/promote?dry_run=true|false` — materializa mappings + precios;
  `dry_run=true` calcula conteos exactos y no persiste nada.

## Qué NO hace esta fase (siguiente paso controlado)

- **No** dispara una sincronización externa en vivo. Lanzar `run_provider_sync(STAGING)` hace
  llamadas de red reales a Open Prices y es la activación sensible; se hace por CLI
  (`python -m cestaplan_api.jobs.sync_price_provider --staging-import`) bajo control explícito, con
  `price_providers_enabled=true` / `open_prices_enabled=true`. Parse.bot y Apify quedan documentados
  pero sin activar (faltan base URLs / actor id).
- **No** activa proveedores por defecto ni pone flags de producción en ningún despliegue: la
  aprobación es una acción humana explícita a través del endpoint.

Ver también [`RECIPE_PROVENANCE_AND_STAGING.md`](./RECIPE_PROVENANCE_AND_STAGING.md) (fase 1).
