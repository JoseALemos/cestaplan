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

## Matriz de cadenas (estado inicial)

| Cadena | Proveedor | Estado | Oficial |
|---|---|---|---|
| DIA | parsebot | `active_when_configured` | no |
| Alcampo | parsebot | `active_when_configured` | no |
| Mercadona | apify | `experimental` | no |
| Open Prices | open_prices | `complementary` (solo observaciones/tickets/validación) | no |
| Carrefour ES | — | `unsupported` | — |
| Lidl ES | — | `partial_source_required` | — |
| Aldi ES | — | `partial_source_required` | — |
| Deza | — | `authorized_feed_required` | — |

No se convierten APIs de Carrefour Francia/Bélgica, Lidl EE. UU. o Aldi Reino Unido en
fuentes para España.

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
