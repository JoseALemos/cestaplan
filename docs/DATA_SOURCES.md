# CestaPlan — Estrategia de fuentes de datos

Este documento describe de dónde salen los datos de CestaPlan (precios, catálogo, nutrición,
alérgenos), qué está permitido hacer con cada fuente, el estado de cada adaptador, las reglas
inviolables de precios, el uso permitido de Open Food Facts, las **licencias por dataset**
(separadas del MIT del código) y el formato CSV de importación.

Es coherente con `docs/DATA_MODEL.md` (entidades `DataSource`, `DataImport`, `ProductPrice`,
`ProductNutrition`) y `docs/ADAPTER_GUIDE.md` (contrato `RetailerAdapter`).

Principio rector: **el presupuesto es una restricción real**. Un precio sólo es utilizable si
tiene **fuente + tienda + fecha**. Nunca se inventan precios. No hay scraping en el MVP.

---

## 1. Tipos de fuente (`source_type`)

Cada dato de precio o catálogo lleva un `source_type` que declara su procedencia y su nivel de
confianza. Son los 9 tipos canónicos:

| `source_type` | Descripción | Confianza típica | Ejemplo |
|---|---|---|---|
| `official` | Datos publicados por la propia cadena mediante un canal oficial y autorizado (API/feed con permiso). | Alta | Feed oficial de una cadena que autorice su uso. |
| `authorized_partner` | Datos provistos por un socio con acuerdo explícito de redistribución. | Alta | Acuerdo con un integrador de datos de retail. |
| `community_connector` | Datos obtenidos por un conector mantenido por la comunidad. **Desactivado por defecto**, activable por flag bajo responsabilidad de quien despliega. | Media/baja | `MercadonaCommunityAdapter` (experimental). |
| `open_dataset` | Conjunto de datos abierto con licencia (p. ej. Open Food Facts). Nutrición/alérgenos/barcode, **no precios como fuente principal**. | Alta (no precio) | Open Food Facts (ODbL). |
| `admin_import` | Carga manual por un administrador (CSV/JSON) de un catálogo del que se dispone legítimamente. | Media/alta | Catálogo importado por el operador. |
| `manual_entry` | Precio introducido a mano por un usuario del hogar para un producto concreto. | Media | El usuario teclea el precio que ve en la tienda. |
| `user_receipt` | Datos derivados de un ticket de compra aportado por el usuario. | Media | Precio leído de un recibo. |
| `estimated` | Valor **estimado** por el sistema (no observado). Nunca se presenta como real. | Baja | Estimación para completar cobertura. |
| `demo` | Datos sintéticos de desarrollo (`is_synthetic=true`). Nunca se presentan como reales. | N/A | Supermercado ficticio de demostración. |

`source_type` es un campo obligatorio de `ProductPrice` y de `DataSource`. La UI diferencia
visualmente los datos `estimated`/`demo` de los reales y nunca los mezcla sin avisar.

---

## 2. Adaptadores y su estado

Todos los adaptadores implementan el **contrato único `RetailerAdapter`** (ver
`docs/ADAPTER_GUIDE.md`). El estado indica si están operativos en el MVP.

| Adaptador | `source_type` dominante | Estado | Activado por defecto | Uso |
|---|---|---|---|---|
| `DemoRetailerAdapter` | `demo` | **Activo** | Sí (sólo desarrollo) | Supermercado ficticio: 150 productos, 50 recetas, precios y nutrición sintéticos. Base del vertical slice. |
| `CsvRetailerAdapter` | `admin_import` | **Activo** | Sí | Importa catálogos/precios desde CSV (formato §5). Vía principal de carga legítima. |
| `JsonRetailerAdapter` | `admin_import` | **Activo** | Sí | Importa desde JSON con el mismo modelo de campos que el CSV. |
| `ManualRetailerAdapter` | `manual_entry` | **Activo** | Sí | Precios introducidos a mano por el usuario para un producto/tienda. |
| `OpenFoodFactsAdapter` | `open_dataset` | **Activo** | Sí | Nutrición, alérgenos, barcode, ingredientes, categorías, marcas e imagen (si licencia). **Nunca precios.** |
| `MercadonaCommunityAdapter` | `community_connector` | **Experimental** | **No (desactivado)** | Conector comunitario de ejemplo. Requiere activación explícita por flag. No scraping ni elusión anti-bot. |
| `AldiRetailerAdapter` | — | Esqueleto | No | Estructura sin implementación. |
| `LidlRetailerAdapter` | — | Esqueleto | No | Estructura sin implementación. |
| `CarrefourRetailerAdapter` | — | Esqueleto | No | Estructura sin implementación. |
| `DiaRetailerAdapter` | — | Esqueleto | No | Estructura sin implementación. |
| `AlcampoRetailerAdapter` | — | Esqueleto | No | Estructura sin implementación. |
| `DezaRetailerAdapter` | — | Esqueleto | No | Estructura sin implementación. |

Cadenas soportadas por el modelo de datos: Mercadona, Aldi, Lidl, Carrefour, Dia, Alcampo,
Deza. Que una cadena esté en el modelo **no** implica que exista un adaptador operativo que
proporcione sus precios: los esqueletos existen para fijar el contrato, no para obtener datos.

> **Regla de seguridad.** Todo conector comunitario:
> 1. viene **desactivado por defecto**;
> 2. se activa sólo por flag de configuración, bajo responsabilidad de quien despliega;
> 3. **no** hace scraping ni elude CAPTCHA/anti-bot;
> 4. marca sus datos como `community_connector` con `confidence_score` acorde.

---

## 3. Reglas de precios (inviolables)

Estas reglas son de cumplimiento obligatorio en cualquier adaptador, importación o cálculo:

1. **Nunca inventar precios.** Si no hay precio con fuente, tienda y fecha, no hay precio.
2. **Ausencia ≠ 0.** Un precio ausente jamás se sustituye por `0`; se marca como `missing` y la
   línea entra en "coste estimado" o queda sin coste conocido.
3. **No confundir precio/kg con precio del envase.** `amount` es el precio del **envase**;
   `unit_price` (p. ej. €/kg) es derivado e informativo. El presupuesto se calcula con envases
   completos y su `amount`.
4. **No presentar estimaciones como reales.** Los datos `estimated` se etiquetan como tales y
   se muestran separados del "coste conocido".
5. **No mezclar tiendas sin avisar.** Un plan se calcula para **una** tienda concreta
   (`store_id`). Si se combinan precios de tiendas distintas, se advierte explícitamente.
6. **No usar datos caducados como actuales.** Si `expires_at < now()`, el precio no es "actual";
   pasa a estado `stale` y contribuye al estado de cobertura `Datos caducados`.
7. **No scraping en el MVP.** No se extraen datos de webs de terceros de forma automatizada.
8. **No eludir anti-bot.** No se sortean CAPTCHA, límites de tasa ni mecanismos de protección.

Estados de cobertura resultantes (canónico §cobertura de precios): **Completo**, **Cobertura
alta**, **Cobertura parcial**, **Cobertura insuficiente**, **Datos caducados**, **Sin datos**.
Cuando falta precio: no se etiqueta el total como exacto; se usan "coste conocido" y "coste
estimado", se puede mostrar un rango, y se ofrece reemplazar el producto o introducir un precio
manual.

---

## 4. Uso permitido de Open Food Facts

Open Food Facts (OFF) es un **dataset abierto** de productos alimentarios. En CestaPlan se usa
**exclusivamente** para datos de producto, **nunca** como fuente principal de precios.

Usos **permitidos** (a través de `OpenFoodFactsAdapter`, `source_type='open_dataset'`):

- Código de barras (EAN/UPC) → `ProductBarcode`.
- Ingredientes declarados → `ProductNutrition.ingredients_text`.
- Alérgenos declarados → `ProductNutrition.allergens` / `traces` (insumo para la validación
  **determinista** de alergias).
- Información nutricional (energía, macros, sal, fibra…) → `ProductNutrition`.
- Imagen del producto → `Product.image_url`, **sólo si la licencia de la imagen concreta lo
  permite** (las imágenes de OFF pueden tener licencias distintas del dato).
- Categorías y marcas → `Product.category_id`, `Product.brand`.

Usos **prohibidos**:

- Usar OFF como fuente de **precios** de venta. OFF no es una fuente de precios fiable ni
  autorizada para ese fin en CestaPlan.
- Redistribuir datos de OFF sin cumplir la atribución y el share-alike de ODbL (ver §5).

---

## 5. Licencias por dataset (separadas del MIT del código)

> **El código de CestaPlan es MIT. Los datos NO heredan esa licencia.** Cada dataset conserva
> su propia licencia y procedencia, documentadas en `DataSource.license_code` y
> `DataSource.attribution_text`. Mezclar la licencia del código con la de los datos sería un
> error legal.

### 5.1 Open Food Facts — ODbL 1.0

La base de datos de Open Food Facts se distribuye bajo **Open Database License (ODbL) 1.0**.
Obligaciones al usar/redistribuir la **base de datos** o trabajos derivados de ella:

- **Atribución.** Reconocer a Open Food Facts como fuente y enlazar a la licencia ODbL. El
  texto de atribución se almacena en `DataSource.attribution_text` y se muestra en la UI/documentación.
- **Share-alike de la base de datos.** Si se distribuye públicamente una base de datos derivada
  (o adaptada) que incorpore datos de OFF, debe ofrecerse bajo ODbL (o compatible).
- **Keep open.** No se pueden aplicar medidas tecnológicas (DRM) que restrinjan a terceros el
  ejercicio de los derechos ODbL sobre la parte de datos de OFF.
- **Imágenes aparte.** Las imágenes de producto en OFF pueden estar bajo licencias distintas del
  dato (p. ej. Creative Commons con condiciones propias). Antes de reutilizar una imagen se
  comprueba su licencia específica; si no consta compatible, no se usa.

Implicación práctica: los datos de nutrición/alérgenos/barcode importados de OFF quedan sujetos
a ODbL, **con independencia** de que el código que los procesa sea MIT.

### 5.2 Datos demo sintéticos

- Generados por el proyecto para desarrollo (`DemoRetailerAdapter`), todos con
  `is_synthetic=true`. Licencia efectiva: la del propio repositorio para contenido sintético
  (sin restricciones de terceros), pero **nunca** se presentan como datos reales ni se usan en
  producción como precios reales.

### 5.3 Catálogos comerciales (importados)

- Los catálogos y listas de precios de cadenas concretas son, por defecto, **no
  redistribuibles**. Un operador puede importarlos para **su propio** despliegue si dispone de
  ellos legítimamente (`admin_import`), pero **no** se publican en el repositorio ni se
  redistribuyen asumiendo permiso.
- Cada importación registra su procedencia en `DataSource`/`DataImport`. Si no consta permiso de
  redistribución, `license_code='proprietary'` y el dato no sale del despliegue del operador.

---

## 6. Formato CSV de importación (sección 20)

`CsvRetailerAdapter` importa precios y catálogo desde CSV UTF-8, con cabecera, separador `,` y
punto decimal. Cada fila es una **observación de precio** de un producto en una tienda; los
campos de producto/tienda permiten crear o resolver las entidades relacionadas. Los importes se
tratan como `Decimal` (nunca float). `JsonRetailerAdapter` acepta los mismos campos en JSON.

Columnas:

| Columna | Obligatoria | Tipo | Descripción |
|---|---|---|---|
| `retailer_slug` | Sí | texto | Slug de la cadena (`mercadona`, `aldi`, …). Resuelve/crea `Retailer`. |
| `store_external_code` | Sí | texto | Código de la tienda dentro de la cadena. Resuelve/crea `Store`. |
| `store_province` | No | texto | Provincia de la tienda. |
| `store_locality` | No | texto | Localidad. |
| `store_postal_code` | No | texto | Código postal (clave de selección de tienda). |
| `product_external_id` | Sí | texto | Id del producto en la fuente. Resuelve/crea `Product`. |
| `product_name` | Sí | texto | Nombre del producto. |
| `brand` | No | texto | Marca. |
| `category` | No | texto | Categoría (slug o nombre). |
| `barcode` | No | texto | EAN/UPC → `ProductBarcode`. |
| `package_quantity` | Sí | numérico | Contenido del envase (p. ej. `500`). |
| `package_unit` | Sí | texto | Unidad del envase (`g`, `kg`, `ml`, `l`, `unit`). |
| `amount` | Sí | numérico | Precio del **envase**. Nunca vacío→0. |
| `currency` | Sí | texto | ISO 4217 (`EUR`). |
| `unit_price` | No | numérico | Precio por unidad base (€/kg, €/l). Derivado; no sustituye a `amount`. |
| `promotion` | No | texto | Descripción de promoción. |
| `availability` | No | texto | `in_stock`, `out_of_stock`, `unknown`. |
| `source_type` | Sí | texto | Uno de los 9 tipos. Para CSV de operador suele ser `admin_import`. |
| `source_name` | Sí | texto | Nombre legible de la fuente. |
| `source_url` | No | texto | Referencia de origen. |
| `observed_at` | Sí | fecha/hora ISO 8601 (UTC) | Momento en que el precio fue observado/vigente. |
| `expires_at` | No | fecha/hora ISO 8601 (UTC) | Caducidad de vigencia del precio. |
| `confidence_score` | No | numérico 0–1 | Confianza. Si se omite, el adaptador aplica un valor por defecto según `source_type`. |
| `verification_status` | No | texto | `unverified` (por defecto), `machine_verified`, `human_verified`, `disputed`. |

Reglas de validación de la importación:

- Filas sin `amount` válido o con `amount<=0` se **rechazan** (nunca se convierte ausencia en 0).
- `observed_at` es obligatorio; sin fecha, la observación no es utilizable como precio.
- Se calcula `imported_at = now()` en la carga; el usuario no lo aporta.
- Cada fila queda enlazada a un `DataImport` (`import_id`) para trazabilidad y posible reversión.
- La combinación `(retailer_slug, store_external_code, product_external_id, observed_at)`
  identifica una observación; reimportar el mismo lote es idempotente vía `checksum` de
  `DataImport`.
- El historial es **append-only**: una nueva fila para el mismo producto/tienda es una nueva
  observación, no un `UPDATE` del precio anterior.

Ejemplo (cabecera + una fila):

```csv
retailer_slug,store_external_code,store_postal_code,product_external_id,product_name,brand,category,barcode,package_quantity,package_unit,amount,currency,unit_price,promotion,availability,source_type,source_name,source_url,observed_at,expires_at,confidence_score,verification_status
demo,DEMO-001,28001,DEMO-CHICKEN-500,Pechuga de pollo bandeja 500 g,MarcaDemo,carnes,,500,g,3.49,EUR,6.98,,in_stock,demo,Supermercado Demo,,2026-07-20T08:00:00Z,2026-08-20T08:00:00Z,1.0,machine_verified
```

---

*Coherencia: este documento sigue las decisiones canónicas de CestaPlan. Ante cualquier
discrepancia, prevalece el fichero canónico de decisiones.*
