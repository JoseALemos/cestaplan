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
