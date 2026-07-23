# Proveedores de precios — arquitectura y política

Capa **agnóstica al proveedor** para incorporar precios de varias fuentes externas sin
acoplar el proyecto a ninguna, sin inventar campos ni precios, y sin presentar ninguna
fuente como oficial de un supermercado. Vive en
`apps/api/src/cestaplan_api/ingestion/providers/` y **reutiliza** la infraestructura de
ingesta existente (cola, worker, scheduler, `HttpFetcher`, validación, anomalías, cobertura,
histórico `PriceObservation`).

## Principios (no negociables)

- **Ninguna fuente es oficial.** Parse.bot no es una API oficial de DIA/Alcampo; los actores
  de Apify no son APIs oficiales de Mercadona; Open Prices es comunitario. `official=false`
  siempre. Ver [`PRICE_DATA_RIGHTS.md`](PRICE_DATA_RIGHTS.md).
- **Todo desactivable por feature flags** (`PRICE_PROVIDERS_ENABLED`, `*_ENABLED`). Todo OFF
  por defecto.
- **Sin scraping directo desde nuestro servidor en esta fase.** Solo se consumen APIs de
  terceros (Parse.bot/Apify) con credenciales del operador, u Open Prices (abierto).
- **Sin secretos en el código.** Tokens por variable de entorno; nunca en URL ni en logs.
- **`canonical_name` es interno** y no se pide al proveedor (se resuelve en la cola de
  revisión, ver `docs/LICENSED_CATALOG.md`).
- **Dinero en `Decimal`**, nunca `float`.
- **No se retira el catálogo demo** hasta cumplir el gate del §14 de la spec.

## Contrato

`PriceCatalogProvider` (`providers/contracts.py`) — `capabilities`, `get_source_metadata`,
`health_check`, `list_stores`, `list_categories`, `iterate_products`, `get_product`,
`iterate_promotions`, `supports_full_catalog/store_scope/incremental_sync`. Todos producen
`ExternalCatalogProduct` (contrato normalizado del §6: `sell_unit`, `net_content_*`,
`price_scope`, `regular_price`/`promotional_price`/`loyalty_price`, `availability`,
`observed_at`, `verification_status`, `confidence_score`…).

Registro: `providers/registry.py` (`registry.get(code)`, `registry.codes()`). El proveedor
`demo` está siempre disponible (fixtures sintéticas, sin red).

## Semántica de cobertura (intención declarada ≠ cobertura observada)

Un error fácil y grave sería marcar una cadena como "cobertura completa" a partir de una
captura de diez registros. Por eso separamos **lo declarado** de **lo observado**, y solo lo
observado (medido de una captura real) decide si una cadena puede costear planes. Los campos
viven en `ProviderActivation` y se exponen en `GET /api/v1/price-providers`:

| Campo | Origen | Significado |
|---|---|---|
| `intended_catalog_scope` | declarado (matriz) | `full` / `partial` / `complementary`: para qué se incorpora la fuente. **No** es evidencia de cobertura. |
| `observed_catalog_scope` | medido | `unknown` / `sample_only` / `partial` / `full`. Una captura que toca el límite, o de una fuente sin catálogo completo, es `sample_only`. |
| `price_coverage` | medido | fracción de la muestra con precio. |
| `package_quantity_coverage` | medido | fracción con cantidad de contenido neto (necesaria para costear por g/ml). |
| `package_unit_coverage` | medido | fracción con unidad de contenido neto. |
| `geographic_scope_coverage` | medido | localización por tienda/zona (0 si la fuente no tiene ámbito de tienda). **No** condiciona el costeo, solo la localización. |
| `costing_eligibility` | derivado | `unknown` / `insufficient` / `sufficient`. `sufficient` exige scope observado real (no muestra) **y** alta cobertura de precio + envase. |
| `production_eligibility` | derivado | solo `true` tras el gate de producción completo + aprobación humana. El onboarding **nunca** lo pone a `true`. |

`measure_coverage()` (en `providers/onboarding.py`) computa estos valores desde los
`ExternalCatalogProduct` capturados; jamás desde la intención.

### Estado de Parse.bot DIA

Salvo que los datos demuestren lo contrario:

- `intended_catalog_scope = full`
- `observed_catalog_scope = sample_only` (captura acotada de ~10 registros)
- `costing_eligibility = insufficient` (sin contenido por envase ni ámbito de tienda)
- `production_eligibility = false`

En la interfaz DIA se muestra como **Experimental** — "datos disponibles para validación,
pero cobertura insuficiente para calcular planes" — y **nunca** como *Disponible* mientras
`costing_eligibility` no sea `sufficient`.

## Matriz de cadenas (estado inicial)

| Cadena | Proveedor | Intención | Estado inicial | Oficial |
|---|---|---|---|---|
| DIA | parsebot | `full` | capturable; `sample_only`, no costeable | no |
| Alcampo | parsebot | `full` | bloqueada (falta base URL) | no |
| Mercadona | apify | `full` | bloqueada (faltan credenciales) | no |
| Carrefour ES | parsebot | `full` | bloqueada (falta base URL) | no |
| Lidl ES | parsebot | `partial` | bloqueada; solo ofertas | no |
| Aldi ES | parsebot | `partial` | bloqueada; solo ofertas | no |
| Deza | parsebot | `partial` | bloqueada; requiere feed autorizado | no |
| Open Prices | open_prices | `complementary` | complementaria (observaciones/validación) | no |
| MercaEjemplo | demo | `complementary` | disponible (fixtures sintéticas) | sí (propio) |

No se convierten APIs de Carrefour Francia/Bélgica, Lidl EE. UU. o Aldi Reino Unido en
fuentes para España. Una fuente `partial` (solo folleto de ofertas) nunca se presenta como el
precio completo de la tienda.

## Alta de las siete cadenas

`python -m cestaplan_api.tools.onboard_all_retailers --limit-per-provider 10 --continue-on-error`
recorre cada proveedor de forma independiente: comprueba configuración (sin exponer secretos),
hace una captura acotada solo donde está configurado, mide la cobertura observada, persiste la
activación (derechos `under_review`, producción nunca activada) e imprime la matriz final. El
fallo de una cadena no bloquea a las demás.

## Plan por fases

1. **FASE 1** ✅ — contrato, `exceptions`, registry, `DemoCatalogProvider`, modelo
   `ProviderUsage` (coste/cuota), flags de config, tests con fixtures.
2. **FASE 2** — `ParseBotClient` + `ParseBotDiaProvider` + fixtures + sync DIA.
3. **FASE 3** — `ParseBotAlcampoProvider` + tiendas + promociones + sync Alcampo.
4. **FASE 4** — `ApifyClient` + `ApifyMercadonaProvider` (flujo asíncrono) + control de coste.
5. **FASE 5** — `OpenPricesProvider` + cobertura + panel admin + cron Railway.

## Credenciales pendientes

`PARSE_BOT_API_KEY`, `APIFY_API_TOKEN`. Sin ellas los proveedores quedan OFF, los tests usan
fixtures y los `@pytest.mark.live` se saltan.
