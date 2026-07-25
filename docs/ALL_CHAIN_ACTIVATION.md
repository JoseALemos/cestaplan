# Activación de todas las cadenas reales (workflow)

Las siete cadenas objetivo y su proveedor efectivo (confirmado contra `RETAILER_MATRIX` + el
registry real):

| Cadena | slug | provider_code (adapter_key) | Provider en registry | Mapper | Estado config local |
|---|---|---|---|---|---|
| Alcampo | `alcampo` | `parsebot-alcampo` | ✅ | ✅ (fp compatible) | URL ✓ · ready_for_probe |
| DIA | `dia` | `parsebot-dia` | ✅ | ✅ | URL ✓ · ready_for_probe |
| Carrefour | `carrefour` | `parsebot-carrefour` | ✅ | ✅ (fp compatible) | URL ✓ · ready_for_probe |
| Lidl | `lidl` | `parsebot-lidl` | ✅ | ✅ (fp compatible) | URL ✓ · ready_for_probe |
| Aldi | `aldi` | `parsebot-aldi` | ✅ | ✅ (fp compatible) | URL ✓ · ready_for_probe |
| Deza | `deza` | `parsebot-deza` | ❌ (matriz sí) | ❌ | URL ✓ · **mapper_missing** |
| Mercadona | `mercadona` | `apify-mercadona` | ❌ (matriz sí) | ❌ | **configuration_missing** (Apify) |

`open-prices` NO se registra como retailer: es una fuente transversal de observaciones.

## Orden de activación (nunca todo a la vez)

1. **Alta de retailers** — `python -m cestaplan_api.tools.bootstrap_retailers --dry-run --all` →
   revisar diff (7 retailers, 0 productos, 0 precios) → `--apply --all`. Idempotente. Crea SOLO
   filas `Retailer` (`is_synthetic=false`); sin tiendas/productos/precios/mappings/activación
   productiva.
2. **ProviderActivation** — fila canónica no productiva por cadena (rights `under_review`, todos los
   gates de producción en `false`). "Cadena dada de alta" ≠ "proveedor operativo".
3. **Gate de red por cadena** — una cadena llega a la red SOLO cuando `parse_bot_enabled` **y**
   `parse_bot_<cadena>_enabled` son true **y** su base URL + key están presentes
   (`parsebot.plans.is_configured`, la única compuerta). Una base URL presente con el flag OFF nunca
   abre red; un flag ON sin URL queda bloqueado.
4. **Secretos en Railway** (servicio `api`) — el titular introduce `PARSE_BOT_API_KEY` + la base URL
   por cadena. No se transfieren claves expuestas.
5. **Probe** (sin escritura) → **sync staging** (1 página, ≤10 productos, `staging_only=true`) →
   **candidatos** (conservadores, sin autoaprobación) → **shadow costing** → **dry-run de
   promoción**. Una cadena por vez.
6. **Promoción a producción** — decisión humana explícita en `/admin/promocion`. Los flags
   productivos permanecen en `false` hasta esa aprobación.

## Notas por cadena

- **Carrefour**: mantener bloqueada la autoaprobación de candidatos si reaparece *candidate
  explosion*.
- **Deza**: `parsebot-deza` no está en el registry ni tiene mapper; queda dado de alta como retailer
  pero no operativo (`mapper_missing`) hasta implementar provider + mapper + fingerprint.
- **Mercadona**: vía Apify, no Parse.bot. Requiere `apify_enabled`, `apify_mercadona_enabled`,
  `apify_api_token`, `apify_mercadona_actor_id` (verificado, no el default a ciegas),
  `apify_mercadona_default_postal_code`, y coste acotado antes de un run. Sin credenciales locales →
  queda `disabled/configuration_missing`.
