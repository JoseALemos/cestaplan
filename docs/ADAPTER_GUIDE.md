# CestaPlan — Guía del adaptador de tiendas (`RetailerAdapter`)

Esta guía define el **contrato único `RetailerAdapter`** que todo conector de tienda debe
implementar, el modelo de selección de tienda, el significado de `confidence_score` y
`verification_status`, y los pasos para añadir un adaptador nuevo.

Es coherente con `docs/DATA_MODEL.md` (`Retailer`, `Store`, `Product`, `ProductPrice`,
`ProductAvailability`, `DataSource`) y `docs/DATA_SOURCES.md` (tipos de fuente, reglas de
precios, licencias).

> **Reglas no negociables para cualquier adaptador**
> - **No scraping** en el MVP y **no elusión** de CAPTCHA/anti-bot/límites de tasa.
> - Todo **conector comunitario** es **desactivable por flag** y **viene desactivado por
>   defecto**.
> - **Nunca inventar precios** ni convertir ausencia en `0`.
> - Todo dato producido lleva `source_type`, `confidence_score` y trazabilidad de origen.

---

## 1. Propósito del contrato

CestaPlan aísla el origen de los datos de tienda tras una interfaz común. El núcleo determinista
(matching, envases, coste, cobertura) no conoce si los datos vienen de un CSV, de Open Food
Facts o de un conector comunitario: sólo habla con `RetailerAdapter`. Esto permite añadir
fuentes sin tocar la lógica de negocio y mantener las reglas de precios centralizadas.

Un adaptador **traduce** una fuente concreta al modelo canónico (`Product`, `ProductPrice`,
`ProductNutrition`, `Store`…). No decide sobre seguridad de alergias, presupuesto ni cálculo de
envases: eso es responsabilidad del motor determinista.

---

## 2. Interfaz común (métodos conceptuales)

Los métodos se describen a nivel conceptual (contrato), no como firma de un lenguaje concreto.
Un adaptador **declara qué sabe hacer** mediante `capabilities()`; los métodos no soportados
devuelven "no soportado" de forma explícita en lugar de fabricar datos.

| Método | Entrada (conceptual) | Salida | Notas |
|---|---|---|---|
| `capabilities()` | — | Conjunto de capacidades y metadatos | Qué operaciones soporta, cadenas cubiertas, si requiere red, si es comunitario, `source_type` por defecto. |
| `metadata()` | — | Identidad del adaptador | `adapter_key`, versión, `DataSource` asociada, licencia, estado (activo/experimental/esqueleto), flag de activación. |
| `search_products(query, store_selector, filters)` | Texto/criterios + selección de tienda | Lista de productos candidatos | Búsqueda de catálogo. Puede paginar. Nunca devuelve precios inventados. |
| `get_product(product_ref, store_selector)` | Referencia de producto | Producto normalizado | Datos de catálogo (nombre, marca, envase, categoría, barcode, nutrición si disponible). |
| `get_price(product_ref, store_selector)` | Producto + tienda | `ProductPrice` (observación) o "sin dato" | Devuelve una observación con fuente y fecha, o indica ausencia. **Jamás** rellena con 0. |
| `get_availability(product_ref, store_selector)` | Producto + tienda | Estado de disponibilidad | `in_stock`/`out_of_stock`/`limited`/`unknown`. |
| `get_store_catalog(store_selector, cursor)` | Selección de tienda | Flujo de productos/precios | Volcado del catálogo de una tienda (importaciones por lotes). |

Contrato transversal de todos los métodos:

- **Idempotencia de lectura.** Consultar no muta el origen.
- **Fail-safe de precios.** Ante ausencia o duda, "sin dato" — nunca un precio fabricado.
- **Trazabilidad.** Todo `ProductPrice` producido incluye `source_type`, `source_name`,
  `source_url` (si aplica), `observed_at`, `confidence_score`, `verification_status` e
  `import_id` cuando procede.
- **Errores tipados.** Distinguir "no soportado", "no encontrado", "fuente no disponible" y
  "dato caducado" en lugar de excepciones opacas.

---

## 3. Modelo de selección de tienda (`store_selector`)

El precio pertenece **siempre** a una tienda concreta. La selección de tienda admite varios
niveles de especificidad; el adaptador resuelve del más específico al más general:

| Nivel | Campo | Descripción |
|---|---|---|
| Cadena | `retailer_slug` | Cadena de supermercado (`mercadona`, `aldi`, …). Obligatorio. |
| Provincia/localidad | `province`, `locality` | Ámbito geográfico. |
| Código postal | `postal_code` | Clave habitual de resolución de tienda. |
| Tienda concreta | `store_external_code` | Código de tienda dentro de la cadena. |
| Id interno | `store_public_id` | `public_id` de `Store` ya conocido en CestaPlan. |
| Fecha de catálogo | `catalog_date` | Fecha de actualización del catálogo a considerar. |
| Cobertura | `min_price_coverage` | Cobertura de precios mínima aceptable para elegir tienda. |

Reglas de resolución:

- Si se da `store_public_id` o `store_external_code`, se usa esa tienda exacta.
- Con sólo `postal_code`/`province`, el adaptador elige la tienda representativa (o devuelve las
  candidatas) y **lo indica**; no se mezclan precios de tiendas distintas sin avisar.
- `catalog_date` acota qué observaciones son "actuales"; las caducadas (`expires_at < fecha`) no
  se presentan como vigentes.
- Si ninguna tienda alcanza `min_price_coverage`, se informa de cobertura insuficiente en lugar
  de completar con estimaciones silenciosas.

---

## 4. `confidence_score` y `verification_status`

Dos ejes independientes describen la calidad de cada `ProductPrice`:

### 4.1 `confidence_score` (0.0 – 1.0)

Confianza numérica en que el dato refleja el precio real vigente. Combina la fiabilidad del
`source_type` y factores del propio dato (antigüedad, completitud). Orientación por origen:

| Origen | Rango orientativo |
|---|---|
| `official`, `authorized_partner` | 0.90 – 1.00 |
| `admin_import` (catálogo del operador) | 0.75 – 0.95 |
| `manual_entry`, `user_receipt` | 0.50 – 0.80 |
| `community_connector` | 0.30 – 0.70 |
| `estimated` | 0.00 – 0.40 |
| `demo` | N/A (dato sintético) |

El adaptador puede degradar la confianza por antigüedad (`observed_at` lejano) o proximidad a
`expires_at`. El optimizador y la UI usan `confidence_score` para priorizar y para decidir qué
mostrar como "conocido" frente a "estimado".

### 4.2 `verification_status`

Estado cualitativo de verificación, ortogonal a la confianza numérica:

| Valor | Significado |
|---|---|
| `unverified` | Sin verificar (por defecto en la mayoría de importaciones). |
| `machine_verified` | Validado por comprobaciones automáticas (rangos, unidad, coherencia). |
| `human_verified` | Confirmado por una persona. |
| `disputed` | Marcado como dudoso/en conflicto; no se usa como actual sin revisión. |

Un adaptador fija ambos campos al producir un precio. `disputed` excluye el dato del cálculo
"actual" hasta su resolución.

---

## 5. Añadir un adaptador nuevo (paso a paso)

1. **Registrar la fuente.** Crear un `DataSource` con `slug`, `source_type`, `adapter_key`,
   `license_code`, `attribution_text` (si la licencia lo exige) e `is_enabled=false` si es
   comunitario.
2. **Definir el `adapter_key`.** Único y estable; es el enlace entre `Retailer.adapter_key`,
   `DataSource.adapter_key` y el registro del adaptador.
3. **Implementar el contrato.** Cubrir al menos `capabilities()` y `metadata()`, y los métodos
   de lectura que la fuente soporte. Los no soportados devuelven "no soportado" explícito.
4. **Normalizar al modelo canónico.** Traducir la respuesta de la fuente a `Product`,
   `ProductPrice`, `ProductNutrition`, `Store`, respetando unidades (envase vs. €/kg), monedas
   y fechas UTC. Dinero como `Decimal`.
5. **Aplicar las reglas de precios.** Nunca inventar; ausencia→`missing` (no 0); fijar
   `source_type`, `confidence_score`, `verification_status`, `observed_at`/`expires_at`.
6. **Declarar la licencia.** Documentar la licencia del dataset en `DataSource` (separada del
   MIT del código). Si no es redistribuible, `license_code='proprietary'` y no se publica.
7. **Flag de activación.** Añadir el flag de configuración. Los conectores comunitarios quedan
   **desactivados por defecto** y sólo se habilitan explícitamente.
8. **Sin scraping / sin anti-bot.** Verificar que el adaptador usa únicamente canales
   autorizados y no elude ninguna protección.
9. **Pruebas.** Tests con datos de ejemplo: normalización correcta, ausencia de precio bien
   señalada, unidades y fechas, idempotencia de importación, y que un flag desactivado impide
   cualquier llamada a red.
10. **Trazabilidad e importación.** Si el adaptador carga por lotes, crear un `DataImport` con
    `checksum`, contadores de filas y `error_report`, y enlazar `import_id` en cada
    `ProductPrice`.
11. **Documentar.** Añadir el adaptador a la tabla de `docs/DATA_SOURCES.md` con su estado y su
    `source_type` dominante.

---

## 6. Estado de los adaptadores y matriz de referencia

| Adaptador | Estado | Activado por defecto | Red requerida | Notas |
|---|---|---|---|---|
| `DemoRetailerAdapter` | Activo | Sí (dev) | No | Datos sintéticos `is_synthetic=true`. |
| `CsvRetailerAdapter` | Activo | Sí | No | Importación por lotes desde CSV. |
| `JsonRetailerAdapter` | Activo | Sí | No | Importación desde JSON. |
| `ManualRetailerAdapter` | Activo | Sí | No | Precios introducidos por el usuario. |
| `OpenFoodFactsAdapter` | Activo | Sí | Sí | Sólo catálogo/nutrición/alérgenos/barcode. **Nunca precios.** ODbL. |
| `MercadonaCommunityAdapter` | Experimental | **No** | Sí | Comunitario, desactivado por defecto. Sin scraping/anti-bot. |
| `Aldi/Lidl/Carrefour/Dia/Alcampo/Deza` | Esqueleto | No | — | Estructura sin implementación. |

Recordatorio final: la existencia de un esqueleto o de un conector comunitario **no** implica
disponibilidad de precios reales de esa cadena. Cualquier conector comunitario es opcional,
desactivable y viene apagado; y en ningún caso se recurre a scraping ni a la elusión de
mecanismos anti-bot.

---

*Coherencia: este documento sigue las decisiones canónicas de CestaPlan. Ante cualquier
discrepancia, prevalece el fichero canónico de decisiones.*
