# Calidad y honestidad de los precios

Cómo el subsistema modela la **calidad** de un precio: su ámbito, su tipo, su
frescura, la cobertura de una tienda/cadena y las anomalías que lo mandan a
cuarentena. El principio subyacente: **nunca presentar parcial como completo, nunca
fabricar un precio, nunca poner `0` por un dato ausente, y siempre `Decimal` para el
dinero.**

Código: `contracts.py`, `validation.py`, `anomaly.py`, `current_price.py`,
`coverage.py`. Ver también: [`PRICE_INGESTION.md`](PRICE_INGESTION.md),
ADR [`0003`](adr/0003-decimal-money.md).

---

## 1. Ámbito del precio — `PriceScope`

A qué área geográfica/administrativa aplica un precio, de más a menos específico. Un
precio **declara** su ámbito; no se asume.

`exact_store` → `delivery_zone` → `postal_code` → `municipality` → `province` →
`region` → `national` → `unknown`.

Regla clave (validación §7): `exact_store` **exige una tienda resuelta**
(`has_store_link`). Si no hay tienda concreta, no se puede reclamar ámbito de tienda
exacta. `unknown` sin declarar es un error de validación, no un valor por defecto
silencioso.

---

## 2. Tipo de precio — `PriceType`

La naturaleza de un precio observado:

| Tipo | Significado |
|------|-------------|
| `regular` | Precio normal de estantería. |
| `promotional` | Precio en promoción (con su `PromotionInfo` asociada). |
| `loyalty` | Precio con tarjeta/fidelización (`requires_loyalty`). |
| `manual` | Introducido manualmente por un administrador. |
| `receipt` | Derivado de un ticket de compra. |
| `estimated` | **Estimado** — nunca se presenta como real. |
| `unknown` | Sin clasificar. |

Las **promociones** se modelan, no se colapsan: `PromotionType` cubre `percentage`,
`fixed`, `nxm` (2x1, 3x2), `second_unit` (2ª ud. al 50 %), `min_quantity` y `pack`.
`PromotionInfo` guarda cantidad requerida/cobrada, descuento, si exige fidelización y
la ventana de validez. Así el precio efectivo se calcula sin perder la regla original.

---

## 3. Frescura — `fresh` / `stale` / `expired`

`CurrentPriceService` (`current_price.py`) enriquece el precio actual con su
**antigüedad** y una frescura, calculada contra dos umbrales:

| Estado | Condición | Umbral (por defecto) |
|--------|-----------|----------------------|
| `fresh` | antigüedad < `STALE_PRICE_HOURS` | < 24 h |
| `stale` | `STALE_PRICE_HOURS` ≤ antigüedad < `EXPIRED_PRICE_HOURS` | 24–48 h |
| `expired` | antigüedad ≥ `EXPIRED_PRICE_HOURS` | ≥ 48 h |
| `unknown` | sin observación válida | — |

Una observación con marca de tiempo futura (antigüedad negativa) se trata como
`fresh`, no como `stale`. La frescura viaja con el precio hasta la API de consumo: la
aplicación puede mostrar "precio de hace N horas" con honestidad.

---

## 4. Cobertura — ratios y 6 estados

`PriceCoverageService` (`coverage.py`) mide **cuánto** del catálogo descubierto tiene
precio usable y lo persiste como `CoverageSnapshot`. La honestidad es el objetivo:
cobertura parcial se reporta como parcial, jamás disfrazada de completa.

### Ratios (`Decimal`)

- `coverage_ratio = priced / expected` — fracción de productos descubiertos con
  precio. (`expected` es hoy el catálogo descubierto activo.)
- `weighted_coverage_ratio = fresh_value / priced_value` — fracción del **valor** con
  precio **fresco** (pondera por importe, no sólo por conteo).

### Estados — `CoverageStatus`

| Estado | Condición |
|--------|-----------|
| `complete` | `coverage_ratio ≥ 1` |
| `high` | `coverage_ratio ≥ 0.9` |
| `partial` | `coverage_ratio ≥ 0.5` |
| `insufficient` | `coverage_ratio < 0.5` |
| `stale` | hay precios pero ninguno fresco (`fresh ≤ 0`) |
| `none` | sin catálogo esperado o sin ningún precio (`expected ≤ 0` o `priced ≤ 0`) |

---

## 5. Anomalías → cuarentena

`AnomalyDetector` (`anomaly.py`) compara el lote recién parseado contra el
**último-bueno** (`PriorStats`) y marca las condiciones de "no confíes en este run".
Toda anomalía severa (≥ `high`) recomienda **quarantine**: el pipeline **nunca**
reemplaza automáticamente el último-bueno con un lote sospechoso.

| Anomalía | Disparo | Severidad |
|----------|---------|:---------:|
| `catalog_drop` | Catálogo cae ≥ 90 % respecto al previo | critical |
| `catalog_growth` | Catálogo crece ×10 respecto al previo | high |
| `price_x100` (`price_spike`) | Precio ×100 (p. ej. 5,49 → 549) | critical |
| `price_x100` (`price_drop`) | Precio /100 | critical |
| `price_spike` / `price_drop` | Movimiento extremo ×3 (o /3) | high |
| `all_same_price` | ≥ 95 % de ≥ 5 productos con el mismo precio | high |
| `empty_catalog` | Catálogo vacío inesperado | high |
| `parser_returned_zero` | El parser no produjo observaciones | high |
| `unit_mismatch` | Cambió el código de unidad de un producto | high |
| `package_change` | Cambió el envase sin cambiar el producto | high |
| `currency_mismatch` | Moneda distinta a la previa | high |
| `zero_or_negative` | Importe ≤ 0 | critical |
| `coverage_drop` | Cobertura cae ≥ 0.3 respecto al día previo | high |
| `block_page` | Respuesta de bloqueo/CAPTCHA/login | critical |

Umbrales configurables en el constructor de `AnomalyDetector`. La validación
por-observación (`validation.py`) añade el corte por respuesta de bloqueo/error
(estados 401/403/407/429/451/500/502/503 → `BLOCK_PAGE`, cuarentena inmediata) y la
coherencia del precio unitario (tolerancia 2 %) que atrapa deslices ×100.

### Qué pasa en cuarentena

`record_observation` (`price_history.py`) guarda la observación cuarentenada como una
fila **cerrada y `disputed`**, ligada a un `PriceAnomaly` con estado `quarantined`.
**La fila abierta del último-bueno se deja intacta**, así que la lectura de precio
actual sigue devolviendo el dato bueno. Un administrador revisa la cuarentena vía
`GET /api/v1/admin/anomalies` y la aprueba o rechaza.

---

## 6. Las reglas de honestidad (resumen)

- **Nunca presentar parcial como completo.** `CoverageSnapshot` reporta `partial` /
  `insufficient` / `stale` cuando corresponde.
- **Nunca fabricar un precio.** Un campo ausente es un error de validación, no un
  valor por defecto. La proyección al motor deja sin precio lo que no tiene dato.
- **Nunca `0` por un dato ausente.** `amount ≤ 0` es `zero_or_negative` → cuarentena.
- **`Decimal` para el dinero**, en todo el pipeline (nunca `float`).
- **Nunca reemplazar el último-bueno** con un lote anómalo: se cuarentena, no se pisa.
- **Las estimaciones no se presentan como reales** (`price_type = estimated`,
  `confidence_score` y `verification_status` viajan con cada observación).

---

## 7. Floors de calidad por-proveedor (§R)

`evaluate_quality` (`ingestion/providers/quality.py`) califica **un sync** de un proveedor
contra los floors §R de `Settings` (`provider_min_price_coverage`, `provider_min_package_coverage`,
`provider_min_observed_at_coverage`, `provider_min_barcode_coverage`) y devuelve
`accepted` / `degraded` / `insufficient` / `quarantined`. Esos floors son **GLOBALES** y son el
valor por defecto para todos los proveedores; **no se relajan globalmente**.

### Override por-proveedor: DIA se costea por precio unitario

Un proveedor puede tener un modelo de dato legítimamente distinto. El conjunto
`UNIT_PRICE_COSTED_PROVIDERS` (en `quality.py`) lista, **con justificación fijada en el código**,
los proveedores cuyo floor de contenido neto (`provider_min_package_coverage`) se satisface con la
cobertura de **precio unitario** en lugar de la de contenido neto:

- **`parsebot-dia`** — el scraper lee la tienda **online** dia.es. Ese endpoint **no** expone
  contenido neto (`net_content_quantity/unit = None`, ver `parsebot/dia.py` §7) pero **sí** publica
  un precio unitario nacional real (`"0,84 €/l"`). Los productos DIA se costean por `unit_price`
  (precio por unidad), no por contenido neto, así que la ausencia de contenido neto **no es un
  defecto** para DIA.

Por qué es **parcial** y defendible: DIA sólo aporta precio unitario nacional online (sin contenido
neto ni tienda física), por eso su costeo es por precio-unitario y su ámbito es `national`
(no `exact_store`). No se baja ningún floor: la cobertura de precio unitario **sustituye** a la de
contenido neto **contra el mismo valor de floor**. Un lote DIA que además careciera de precios
unitarios seguiría fallando el floor (`insufficient`). Cualquier otro proveedor conserva el
contenido neto como única vía para superar el floor de package.

### Qué consume el override de `evaluate_quality`

El grade §R de `evaluate_quality` alimenta `SyncReport.quality_status` en
`services/provider_sync.run_sync`: decide si un sync **escribe** o se **cuarentena**. Con el
override, un sync DIA (`scope=national` §6, precio unitario presente, contenido neto 0) queda sin
`reasons` → `quality_status = accepted`, así que ya **no** se bloquea por
`package_coverage_below_floor` ni `geographic_scope_undeterminable`.

### Cómo se asigna `activation.data_quality_status` (traza exacta)

`activation.data_quality_status` lo escribe **exclusivamente** `onboarding.upsert_activation`
(`ingestion/providers/onboarding.py`, parámetro `data_quality_status`). Su único llamador de
producción es `tools/onboard_all_retailers._onboard_one`, que **NO** deriva ese valor de
`evaluate_quality` sino de `measure_coverage(...).costing_eligibility`:

```python
data_quality_status="accepted" if coverage.costing_eligibility == "sufficient" else (
    "degraded" if coverage.observed_catalog_scope != "unknown" else "insufficient")
```

Ese estado es el que consumen `provider_promotion._production_prerequisite_reasons` /
`_production_gate_reasons` y `activation.evaluate_production` como precondición de producción. Los
**otros** gates de proceso (mapper `verified`, transport, aprobación manual, production flags, ≥3
syncs, rollback) se despejan operativamente aparte y **no** se tocan aquí.

### Excepción de costeo scoped a DIA (precio-unitario nacional)

`costing_eligibility` sale de `classify_costing_mode` (producto fresco) y
`classify_variant_costing_mode` (variante almacenada), ambas en `onboarding.py`. Por invariante
general, un `unit_price` de referencia suelto sobre un paquete de contenido desconocido es
`UNRESOLVED`. **Excepción scoped** para los proveedores de `UNIT_PRICE_COSTED_PROVIDERS` (DIA): si
el `unit_price_unit` es una unidad de masa/volumen conocida (`kg`/`g` → `VARIABLE_WEIGHT`,
`l`/`ml` → `VARIABLE_VOLUME`) se costea por ese precio unitario nacional aunque no haya contenido
neto; una unidad ausente o ajena sigue siendo `UNRESOLVED` (nunca se adivina). Justificación: DIA
sólo da precio unitario nacional online, sin contenido neto en la búsqueda ni tienda física, por
eso su costeo es por `price_per_unit` y sólo con unidades compatibles.

- `classify_variant_costing_mode` recibe `provider_code: str | None = None`; con el **default
  `None` el comportamiento es idéntico al histórico** (cero regresión para callers no actualizados).
  Se pasa el `provider_code` real en los call sites scoped a un proveedor: `recipe_costing`
  (planner), `recipe_catalog_coverage` (cobertura → `data_quality`), `provider_shadow`,
  `mapping_review`. `measure_coverage` usa `classify_costing_mode(p)` y lee `p.provider`
  directamente, así que no necesita el parámetro.
- Con esta excepción, un re-onboard de DIA (scope national, `unit_price` €/l presente, contenido
  neto None) da `costing_eligibility = sufficient` → `_onboard_one` escribe
  `data_quality_status = "accepted"`. Ésta es la vía por la que DIA llega a `accepted`.

> **Gaps operativos pendientes (NO resueltos en este cambio).** Para que el costeo DIA sea posible
> Y numéricamente exacto de extremo a extremo en producción faltan dos piezas del camino de sync
> (`services/provider_sync.py`), que se dejan señaladas al owner:
> 1. `_upsert_variant` **no persiste** `variant.unit_price` / `unit_price_unit` (sólo sell_unit y
>    net content). Sin ese dato, la variante DIA almacenada tiene `unit_price = None` y
>    `classify_variant_costing_mode` la vería `UNRESOLVED`. Los caminos `targeted_discovery` y
>    `licensed_catalog` sí lo persisten; el sync principal no.
> 2. `_append_observation` guarda `amount = product.regular_price` (precio de estante), mientras
>    que el costeo `VARIABLE_*` aguas abajo (`_cost_candidate`) usa ese `amount` como precio por
>    unidad base. Coinciden sólo cuando el envase equivale a 1 unidad del `unit_price_unit` (p. ej.
>    leche 1 L). Para un costeo exacto en tamaños arbitrarios, el sync debería persistir/usar el
>    `unit_price` (€/l) de DIA. No se toca el costeo aguas abajo aquí por indicación del owner.
