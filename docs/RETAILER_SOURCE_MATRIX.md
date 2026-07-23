# Matriz de fuentes por supermercado

Estado **honesto** de cada supermercado como fuente de precios. Este documento no
promete lo que no existe: **no** hay API oficial y legal de precios por tienda de
ninguna cadena; donde una fuente prohíbe el acceso a sus datos, su conector queda
`permission_required` (framework presente, **desactivado**, nunca rastreado).

Reglas transversales (ver [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md)):

- **No se realiza scraping** de fuentes bloqueadas ni se eluden CAPTCHA/anti-bot/login.
- **`robots.txt` no es una autorización.** Que una ruta esté permitida en `robots.txt`
  no habilita por sí solo su ingesta; se evalúan además los términos de uso.
- Una fuente bloqueada o con autenticación se **detiene y se marca**, nunca se evade.

Estados legales (`LegalStatus`, `contracts.py`): `public` · `authorized` ·
`permission_required` · `prohibited` · `unknown`.
Estados de conector (`ConnectorStatus`): `active` · `partial_only` · `unsupported` ·
`permission_required` · `disabled` · `degraded` · `temporarily_blocked` ·
`parser_broken` · `source_unavailable`.

---

## Matriz

| Supermercado | Fuente | Tipo de fuente | Catálogo | Precios | Promociones | Ámbito | Frecuencia | Autorización | Riesgos | Estado del conector | Última revisión |
|--------------|--------|----------------|----------|---------|-------------|--------|-----------|--------------|---------|---------------------|:---------------:|
| **Demo (DemoFixtureMart)** | Fixtures sintéticas locales | `demo` | Completo (~26 productos) | Sí | Sí (2x1, 3x2, 2ª ud., %) | `exact_store` (1 tienda) | On-demand | `public` (dato sintético) | Ninguno (sin red) | **`active`** — siempre registrado | 2026-07-22 |
| **Open Prices** | Dataset abierto Open Prices (OFF) | `open_dataset` | Parcial (contribución comunitaria) | Sí (reales) | Limitado | Variable / `national`/tienda según dato | Cron diario | `public` — **ODbL** (con atribución) | Cobertura desigual por zona | **Implementado** como `OpenPricesConnector` en el framework (FASE C); también sincronizable por cron `sync_open_prices` | 2026-07-22 |
| **Mercadona** | Web / endpoints internos | (conector comunitario) | — | — | — | — | — | **`permission_required`** — `robots.txt`: `Disallow: /api` prohíbe sus endpoints de datos | Prohibición explícita; anti-bot | **`permission_required`** — framework presente, **desactivado**, nunca rastreado | 2026-07-22 |
| **Carrefour** | Web / AJAX de supermercado | (conector comunitario) | — | — | — | — | — | **`permission_required`** — `robots.txt`: `Disallow: /supermercado/ajax/*` prohíbe sus endpoints de datos | Prohibición explícita; anti-bot | **`permission_required`** — framework presente, **desactivado**, nunca rastreado | 2026-07-22 |
| **Lidl** | Folletos de ofertas (semanal) | `LidlOffersConnector` (conector de ofertas, FASE E) | **Parcial (solo ofertas)** — nunca catálogo completo | Solo precios de oferta | Sí — con **fechas de validez** (`valid_from`/`valid_until`), 2x1, %, fidelidad | `national` (folleto) | — | **`permission_required`** — `robots.txt`: `Disallow: /user-api/*`; sin fuente pública autorizada | Prohibición explícita; anti-bot | **`permission_required`** — implementado, **desactivado** por defecto (`LIDL_OFFERS_CONNECTOR_ENABLED=false`), nunca rastrea | 2026-07-22 |
| **Aldi** | Folletos de ofertas (semanal) | `AldiOffersConnector` (conector de ofertas, FASE E) | **Parcial (solo ofertas)** — nunca catálogo completo | Solo precios de oferta | Sí — con **fechas de validez**, 2x1, %, fidelidad | `national` (folleto) | — | **`permission_required`** — sin fuente pública autorizada | anti-bot | **`permission_required`** — implementado, **desactivado** por defecto (`ALDI_OFFERS_CONNECTOR_ENABLED=false`), nunca rastrea | 2026-07-22 |
| **Deza** | Import de operador (admin) | `DezaOffersConnector` (FASE E) | **Parcial** vía import | Vía import de operador | — | Regional (Galicia) | — | **`permission_required`** — sin fuente pública autorizada | Volumen bajo; sin fuente pública | **`unsupported`** (scraping) — su ruta real es **import por admin**; **desactivado** por defecto (`DEZA_CONNECTOR_ENABLED=false`), nunca rastrea | 2026-07-22 |
| **Dia** | Páginas de catálogo públicas | (por evaluar) | Parcial (algunas páginas de catálogo permitidas) | Por evaluar | Por evaluar | Por determinar | — | **Por evaluar** — parte del catálogo aparece permitida en `robots.txt`; términos pendientes de revisión | Fragilidad de HTML; términos no confirmados | **`partial_only`** — evaluación por fuente pendiente, sin activar | 2026-07-22 |
| **Alcampo** | `compraonline.alcampo.es` | (por evaluar) | Parcial | Por evaluar | Por evaluar | `delivery_zone` (compra online) | — | **Ambigua** — `robots.txt` ambiguo; requiere revisión de términos antes de cualquier ingesta | Ambigüedad legal; anti-bot posible | **`partial_only`** / `unsupported` — pendiente, sin activar | 2026-07-22 |
| **Entrada manual** | Operador (admin) | `record_manual_price` (`ingestion/manual_entry.py`, FASE E) | — | Sí — precio tecleado por operador (`price_type=manual`) | — | `exact_store` (con tienda) o `national` | On-demand | `authorized` — dato del operador, **no** scraping | Ninguno (sin red) | **`active`** — `POST /api/v1/admin/prices/manual` (admin + CSRF), append-only y auditado | 2026-07-22 |

> Notas sobre `robots.txt`: las rutas citadas (Mercadona `Disallow: /api`, Carrefour
> `Disallow: /supermercado/ajax/*`, Lidl `Disallow: /user-api/*`) son la razón por la
> que sus endpoints de datos quedan **fuera de alcance**. La cadena expone el catálogo
> al navegador, pero **prohíbe** el acceso programático a esos endpoints; por eso el
> conector existe como framework pero **no se ejecuta**.

---

## Detalle por fuente

### Demo (`DemoFixtureMart`) — `active`

Retailer sintético `demofixturemart` (`ingestion/connectors/demo.py`). ~26 productos
que abarcan masa/volumen/unidad, multipacks y promociones, con escenarios
deterministas (`baseline`, `price_change`, `anomaly`, `catalog_drop`, `block_page`).
No toca la red: existe para ejercitar el vertical completo (fetch → … → coverage) en
tests y demos. Siempre registrado (`DEMO_ALWAYS_ENABLED`), sin depender de flags.

### Open Prices — primer conector real implementado (`open_dataset`, ODbL)

Fuente **pública, real y legal** bajo licencia **ODbL** (con atribución). Es una base
de datos colaborativa de precios (proyecto Open Food Facts). Hoy se ingiere como
`DataSource` de tipo `open_dataset` mediante `python -m
cestaplan_api.scripts.sync_open_prices` (cron `open-prices-sync`, ver
[`RAILWAY_PRICE_SYNC.md`](RAILWAY_PRICE_SYNC.md)); su envoltura como
`RetailerConnector` del framework de ingesta está **implementado** como **primer conector
real**. Cobertura desigual: se reporta con honestidad vía `CoverageSnapshot`, nunca
como completa. Es la única fuente de precios reales activa del proyecto.

### Mercadona / Carrefour — `permission_required`

El framework del conector existe (contrato, política, estado), pero está
**desactivado** y **nunca rastrea**. La razón es concreta y verificable: sus
`robots.txt` **prohíben** el acceso a los endpoints de datos (ver rutas arriba).
Activar `MERCADONA_CONNECTOR_ENABLED` / `CARREFOUR_CONNECTOR_ENABLED` **no** los pone
a rastrear: la política los mantiene detenidos hasta obtener permiso explícito de la
cadena. `robots.txt` no se trata como autorización.

### Lidl / Aldi — conectores de **ofertas** (`permission_required`, FASE E)

`LidlOffersConnector` y `AldiOffersConnector` (`ingestion/connectors/offers.py`) están
**implementados** pero son fuentes **PARCIALES de OFERTAS**, **no** catálogos
completos: `capabilities().full_catalog=False`, `promotions=True`, `prices=False`
(las ofertas llevan precios promocionales, no un catálogo regular completo). Cada
observación es `promotional` (o `loyalty` con tarjeta) y lleva un `PromotionInfo` con
**fechas de validez** (`valid_from`/`valid_until`), nunca colapsado a un precio unitario.

Son **honestos y legales**: **desactivados por defecto**
(`LIDL_OFFERS_CONNECTOR_ENABLED` / `ALDI_OFFERS_CONNECTOR_ENABLED = false`) y **nunca
rastrean**. No hay fuente pública autorizada de sus folletos (Lidl `Disallow:
/user-api/*`), así que **todo camino en vivo** (`health_check`, `fetch_*`) devuelve un
resultado controlado `permission_required` **sin realizar ninguna petición HTTP**. El
único camino que produce observaciones es un **fixture sintético** de ofertas (dicts
Python, sin HTML/PDF real) usado en tests o, en producción, cuando un operador aporta
un feed de ofertas **autorizado**. Nunca se presenta una fuente de ofertas como cobertura
del supermercado completo.

### Deza — `unsupported` (scraping) → **import por admin** (FASE E)

`DezaOffersConnector` está implementado, **desactivado por defecto**
(`DEZA_CONNECTOR_ENABLED=false`). Deza es una cadena regional pequeña **sin fuente
pública soportada**: el scraping es `unsupported` (todo camino en vivo devuelve un
resultado controlado `unsupported`, **sin petición HTTP**). Su **ruta real** de datos
es un **import por admin** (o un feed de ofertas autorizado aportado al conector). Su
footing legal es `permission_required`, así que **no puede activarse**.

### Dia / Alcampo — `partial_only` (por evaluar)

Evaluación **por fuente pendiente**. Dia permite parte del catálogo en `robots.txt`
pero sus términos no están confirmados. Alcampo (`compraonline.alcampo.es`) tiene
`robots.txt` ambiguo y requiere revisión de términos antes de cualquier ingesta.
Ninguno está activo; ninguno se rastrea mientras la evaluación no concluya y no exista
base legal.

### Entrada manual de precios — `active` (FASE E)

`record_manual_price` (`ingestion/manual_entry.py`) permite que un operador registre un
precio observado (ticket de estantería, llamada a la tienda) como `PriceObservation` de
primera clase con `price_type=manual`. **No fabrica** nada: valida el importe (Decimal
> 0) y la moneda, usa `exact_store` solo cuando se aporta una tienda (si no, `national`),
es **append-only** (reutiliza `record_observation`), se **proyecta** al motor
(`ProductPrice`) y queda **auditado** (`AuditLog`). Interfaz sección-17:
`POST /api/v1/admin/prices/manual` (requiere admin + CSRF).

---

## Trazabilidad de la revisión

Cada `DataSource` guarda su paper trail de cumplimiento (`SourceAuditService`,
`ingestion/audit.py`): `legal_status`, `terms_reviewed_at`, `robots_reviewed_at` y
`notes`. Consultable en `GET /api/v1/admin/sources`. Esta matriz es el resumen
legible; la fuente de verdad operativa es ese registro por fuente.
