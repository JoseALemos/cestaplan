# Guía de fuentes de precios

CestaPlan trata el precio como un **dato con procedencia**, no como una cifra
suelta. Esta guía explica cómo aportar precios mediante importación CSV/JSON,
qué reglas son obligatorias y qué licencias respetar.

> Principio central: **el presupuesto es una restricción real**. Un precio sin
> **fuente + tienda + fecha** no sirve. **Nunca se inventan precios.**

## Reglas obligatorias

- **Nunca inventar precios.** Si no tienes el dato, no lo aportes.
- **Fuente + tienda + fecha siempre.** Cada precio identifica de dónde sale, en qué
  tienda concreta y cuándo se observó.
- **No scraping.** Nada de *scraping* ni de eludir CAPTCHA/anti-bot. Los datos deben
  provenir de una fuente legítima (oficial, autorizada, dataset abierto, import de
  admin, entrada manual, ticket de usuario, etc.).
- **No sustituir ausente por `0`.** Un precio ausente es "sin dato", no "cero".
- **No confundir precio por kg con precio del envase.** `amount` es el precio del
  envase; `unit_price` es el precio por unidad de medida.
- **No presentar estimaciones como reales.** Usa `source_type=estimated` y
  `verification_status` en consecuencia.
- **No mezclar tiendas sin avisar** ni usar **datos caducados** como actuales
  (`expires_at`).
- **Dinero exacto.** Importa `amount`/`unit_price` como cadenas decimales (p. ej.
  `"3.49"`), nunca como `float`. Internamente son `Decimal`/`numeric`.

## `source_type` permitidos

Solo estos valores son válidos:

| `source_type` | Uso |
|---------------|-----|
| `official` | Fuente oficial del retailer (feed/API autorizada). |
| `authorized_partner` | Socio con permiso para compartir datos. |
| `community_connector` | Conector comunitario (desactivado por defecto). |
| `open_dataset` | Dataset abierto con licencia compatible. |
| `admin_import` | Importación manual del administrador (CSV/JSON). |
| `manual_entry` | Precio introducido a mano en la app. |
| `user_receipt` | Precio tomado de un ticket real del usuario. |
| `estimated` | Estimación explícita (no es un dato real). |
| `demo` | Dato sintético de demostración (`is_synthetic`). |

## Columnas del formato de importación

Cada fila corresponde a un `ProductPrice`. Columnas (obligatorias salvo indicación):

| Columna | Obligatorio | Descripción |
|---------|-------------|-------------|
| `retailer_id` | Sí | Identificador de la cadena. |
| `store_id` | Sí | Tienda concreta (no solo la cadena). |
| `product_id` | Sí | Producto al que aplica el precio. |
| `amount` | Sí | Precio del **envase** como cadena decimal (`"3.49"`). |
| `currency` | Sí | Moneda ISO-4217 (p. ej. `EUR`). |
| `package_quantity` | Sí | Cantidad del envase (p. ej. `500`). |
| `package_unit` | Sí | Unidad del envase (`g`, `ml`, `unit`). |
| `unit_price` | Sí | Precio por unidad de medida como cadena decimal. |
| `promotion` | No | Descripción/valor de promoción, si aplica. |
| `availability` | Sí | Disponibilidad observada. |
| `source_type` | Sí | Uno de los valores de la tabla anterior. |
| `source_name` | Sí | Nombre legible de la fuente. |
| `source_url` | No | URL de la fuente, si existe. |
| `observed_at` | Sí | Fecha/hora (UTC, ISO-8601) en que se observó el precio. |
| `imported_at` | Sí | Fecha/hora (UTC) de la importación. |
| `expires_at` | No | Cuándo deja de considerarse actual. |
| `confidence_score` | No | Confianza en el dato (0–1). |
| `import_id` | Sí | Identificador del lote de importación. |
| `verification_status` | Sí | Estado de verificación del precio. |

### Ejemplo CSV

```csv
retailer_id,store_id,product_id,amount,currency,package_quantity,package_unit,unit_price,promotion,availability,source_type,source_name,source_url,observed_at,imported_at,expires_at,confidence_score,import_id,verification_status
demo-market,demo-store-001,prod-chicken-500,3.49,EUR,500,g,6.98,,in_stock,demo,Demo Market Synthetic,,2026-07-20T10:00:00Z,2026-07-21T09:00:00Z,2026-08-20T00:00:00Z,1.0,imp-demo-001,verified
```

### Ejemplo JSON

```json
{
  "import_id": "imp-demo-001",
  "prices": [
    {
      "retailer_id": "demo-market",
      "store_id": "demo-store-001",
      "product_id": "prod-chicken-500",
      "amount": "3.49",
      "currency": "EUR",
      "package_quantity": 500,
      "package_unit": "g",
      "unit_price": "6.98",
      "promotion": null,
      "availability": "in_stock",
      "source_type": "demo",
      "source_name": "Demo Market Synthetic",
      "source_url": null,
      "observed_at": "2026-07-20T10:00:00Z",
      "imported_at": "2026-07-21T09:00:00Z",
      "expires_at": "2026-08-20T00:00:00Z",
      "confidence_score": 1.0,
      "verification_status": "verified"
    }
  ]
}
```

> Los precios se **insertan** como histórico (no `UPDATE` destructivo): cada
> observación es una fila nueva con su `observed_at`.

## Cobertura de precios

El motor calcula la cobertura del plan a partir de los precios disponibles:

- `price_coverage` = líneas con precio válido / líneas totales.
- `weighted_price_coverage` = valor conocido estimado / valor total aproximado.
- Estados posibles: **Completo**, **Cobertura alta**, **Cobertura parcial**,
  **Cobertura insuficiente**, **Datos caducados**, **Sin datos**.

Aportar precios de buena procedencia mejora la cobertura y evita que un plan tenga
que apoyarse en "coste estimado".

## Licencias

- Indica siempre la **procedencia y licencia** de los datos que aportas (ver
  [DATA_SOURCES.md](./DATA_SOURCES.md)).
- No subas **catálogos comerciales** asumiendo que puedes redistribuirlos.
- **Open Food Facts** se usa solo para código de barras, ingredientes, alérgenos,
  nutrición, categorías, marcas e imagen (si la licencia lo permite), **nunca como
  fuente principal de precios**, y bajo **ODbL** (atribución + *share-alike*).

Ver también: [ADAPTER_GUIDE.md](./ADAPTER_GUIDE.md) ·
[RECIPES_GUIDE.md](./RECIPES_GUIDE.md) · [/CONTRIBUTING.md](../CONTRIBUTING.md).
