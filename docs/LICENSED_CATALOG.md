# Catálogo licenciado y gate de sustitución de la demo

Subsistema **agnóstico al proveedor** para incorporar catálogos/feeds de precios reales
**con derechos** (feed comercial contratado o catálogo licenciado), mapearlos a los
ingredientes del recetario mediante revisión humana, y decidir de forma **auditable** cuándo
una cadena real puede **sustituir** al catálogo de demostración.

> **Regla de oro:** el catálogo demo (`MercaEjemplo`) y los imports de muestra **no se
> retiran** hasta que el *gate* de la [FASE 5](#fase-5--gate-de-salida) pase para la cadena
> **y** la licencia esté firmada. Ninguna parte del sistema inventa precios ni resuelve
> `canonical_name` a partir del proveedor.

## Arquitectura por fases

| Fase | Qué añade | Código |
|---|---|---|
| **1 · Contrato de datos** | `ProductVariant` con `sell_unit`, `net_content_quantity/unit`, `variable_weight`, `unit_price/unit`; `IngredientProductMapping` con `product_variant_id`, `match_method`, `verification_status`, `verified_by/at`; modelo `SupplierFieldMapping`. | `models/ingestion.py`, `models/catalog.py`, migración `f549b465d14b` |
| **2 · Importadores agnósticos** | `SupplierFieldMap`+`resolve_record` (rutas con puntos, dinero `Decimal`, alias de unidad), `Csv`/`JsonLicensedCatalogImporter`, `persist_records` (idempotente, histórico append-only, `dry_run`). | `ingestion/licensed_catalog.py` |
| **3 · Import de muestra (10 pasos)** | `run_sample_import` → `SampleImportReport`: schema validation, dry-run, informe de errores, cobertura, dedup, normalización de unidades, validación de precios, validación geográfica, candidatos de mapeo, cola de revisión. | `services/sample_import.py` |
| **4 · Mapeo y revisión** | API admin: `field-mappings`, `sample-import`, `review-queue`, `review/{id}/approve\|reject`, `coverage`. | `routers/licensed_admin.py` |
| **5 · Gate de salida** | `evaluate_readiness` + `GET /readiness/{retailer_id}`: evalúa los 8 criterios y `can_retire_demo`. | `services/readiness.py` |

## El `canonical_name` es interno

El proveedor **no** aporta `canonical_name`: pertenece a la taxonomía interna de NutriPlan.
La asociación producto↔ingrediente la propone el matcher conservador (umbral 0.70) como
candidato **inactivo** y `machine_verified`, y **solo** se activa (`human_verified`,
`is_active=true`, `verified_by/at`) al aprobarla un humano en la cola de revisión. Un rechazo
la marca `disputed` e inactiva. El planificador solo usa mapeos activos y verificados.

## Contrato de envases

`sell_unit ∈ {package, unit, weight, volume}` describe *cómo se vende*; el **contenido neto**
(`net_content_quantity` + `net_content_unit ∈ {g, kg, mg, ml, l, cl}`) es lo que permite
costear una receta en g/ml aunque el producto se venda "por unidad". `variable_weight` marca
productos a peso variable (su precio es un `unit_price`/`unit_price_unit`). El dinero es
siempre `Decimal`, nunca `float`.

## FASE 5 · Gate de salida

`evaluate_readiness(db, retailer, GateConfig(min_ingredient_coverage, license_verified))`
mide los ocho criterios; `can_retire_demo` es `True` **solo si todos pasan**:

| # | Criterio | Cómo se comprueba |
|---|---|---|
| 1 | Contrato de licencia verificado | `license_verified` (atestación del operador; un contrato no se verifica por código) |
| 2 | Mínimo de cobertura acordado | cobertura de ingredientes verificados ≥ `min_ingredient_coverage` |
| 3 | Mapeo de campos validado | existe un `SupplierFieldMapping` activo con los campos requeridos |
| 4 | Actualización incremental | ≥1 variante con >1 observación (un segundo sync añadió) |
| 5 | Idempotencia | ninguna `(variante, scope, tienda)` con >1 observación abierta |
| 6 | Histórico | ≥1 observación cerrada (`valid_until` fijado) |
| 7 | Cobertura de ingredientes medida | ingredientes `human_verified` activos > 0 |
| 8 | Cero errores críticos de unidad/dinero | sin importes ≤ 0 y sin mapeo activo sobre unidad no costeable |

**Uso (admin):**

```
GET /api/v1/admin/licensed/readiness/{retailer_id}?min_coverage=0.6&license_verified=true
```

Solo cuando la respuesta trae `"can_retire_demo": true` y la licencia está firmada procede
retirar `MercaEjemplo`/los imports de muestra.

## Flujo operativo

1. `POST /field-mappings` — declarar el mapa del proveedor (una vez por fuente).
2. `POST /sample-import` (`dry_run=true`) — subir una muestra y revisar el `SampleImportReport`.
3. Corregir el mapa/datos hasta `ok=true` y cobertura suficiente; repetir en `dry_run=false`.
4. `GET /review-queue` → `approve`/`reject` los candidatos.
5. Segundo sync (datos actualizados) para demostrar incremental + histórico.
6. `GET /readiness/{retailer_id}` → si `can_retire_demo=true` **y licencia firmada**, retirar la demo.
