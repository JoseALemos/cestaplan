# CestaPlan — PRD (Product Requirements Document)

> Documento de requisitos de producto del MVP. Fuente de verdad de decisiones:
> `docs/` + el fichero canónico de decisiones. Toda afirmación aquí debe ser
> consistente con los principios no negociables. Prosa en español; identificadores
> y claves en inglés.

---

## 1. Promesa del producto

> **"Dime dónde compras, cuánto quieres gastar, para cuántas personas y qué comidas
> necesitas. CestaPlan genera recetas, calcula los envases necesarios y prepara una
> lista de compra adaptada a una tienda concreta."**

CestaPlan es un planificador de comidas con conciencia de presupuesto. El usuario declara
un contexto (tienda, presupuesto, personas, comidas, restricciones) y el sistema produce un
**plan de comidas + lista de compra** calculada sobre **envases completos** y precios con
procedencia. La inteligencia artificial **propone**; el **núcleo determinista valida y calcula**.

### Qué es y qué NO es el valor del MVP

- **Es**: planificación de comidas + cálculo de envases y coste **sobre los datos que se aportan**
  (supermercado demo sintético, importación CSV/JSON o entrada manual de precios). El resultado es
  reproducible, auditable y funciona **sin OpenAI**.
- **No es**: un comparador de precios en vivo entre supermercados reales. En el MVP **no hay scraping**
  ni integraciones oficiales de precios listas para producción. La calidad del coste depende de la
  calidad de los datos que el usuario o el administrador cargan.

Esta distinción es deliberada y debe comunicarse en el producto: el coste se etiqueta según su
**cobertura de precios** (ver §12), nunca se presenta un estimado como precio real.

---

## 2. Problema y usuarios objetivo

### 2.1 Problema

Planificar comidas con un presupuesto real es tedioso y propenso a error:

- Las recetas piden cantidades (600 g de pollo) pero las tiendas venden **envases** (bandejas de 500 g).
  Calcular `600/500 × precio` es incorrecto: se compran envases enteros y sobra producto.
- Las **alergias** son una restricción de seguridad, no una preferencia. Un LLM no puede decidirlas.
- Los **precios** cambian por tienda, fecha y promoción. Inventarlos o mezclarlos entre tiendas engaña.
- Las familias y hogares compartidos necesitan **flexibilidad**: huecos, tuppers, cocinar para varios días.

### 2.2 Personas objetivo

| Persona | Descripción | Necesidad dominante |
|---|---|---|
| **1 persona** | Vive sola, cocina para sí, quiere tuppers para el trabajo | Comidas rápidas, sin desperdicio, coste controlado |
| **Pareja** | Dos adultos, gustos compartidos con matices | Raciones para dos, reutilización de ingredientes |
| **Familia** | Adultos + menores, alergias/intolerancias frecuentes | Restricciones duras, comidas familiares, batch cooking |
| **Hogar compartido** | Varios adultos con presupuestos y preferencias distintos | Permisos por hogar (owner/editor/viewer), planes por miembro |

El modelo de datos soporta múltiples miembros por hogar (`Household`, `HouseholdMember`) con roles
`owner`, `editor`, `viewer`.

---

## 3. Principios no negociables

1. **El presupuesto es una restricción real.** Cada precio lleva **fuente + tienda + fecha**. Nunca se inventa un precio.
2. **Las alergias son una restricción DURA.** El LLM **no** hace cálculos económicos ni decide seguridad de alergias.
3. **OpenAI propone; el núcleo determinista valida y calcula.** Planificación reproducible y auditable.
4. **Toda función crítica funciona sin OpenAI.** Mobile-first PWA. No es consejo médico.
5. **Dinero siempre exacto.** `Decimal` en Python / `numeric` en Postgres. En JS el dinero viaja como **string**. Nunca `float`.
6. **Sin scraping en el MVP.** No se elude CAPTCHA/anti-bot. Los conectores comunitarios están **desactivados por defecto**.
7. **Cálculo por envases completos.** Se compra el envase entero; se rastrea sobrante, coste imputable y coste marginal.
8. **Cobertura de precios explícita.** El coste se etiqueta (Completo / Cobertura alta / … / Sin datos), nunca "exacto" si faltan precios.
9. **Privacidad por diseño.** Nunca se envían a OpenAI nombres reales, email ni identificadores internos: el contexto se pseudonimiza.

---

## 4. Alcance del MVP vs fuera de alcance

### 4.1 Dentro del MVP

- Registro/login con sesiones opacas en BD, hogar con miembros y roles.
- Perfil dietético, alergias, restricciones, preferencias, equipamiento.
- Selección de **tienda concreta** (cadena + provincia/localidad + CP + tienda + id interno + fecha de catálogo).
- Fuentes de datos: **demo sintético**, **CSV**, **JSON**, **entrada manual**, **Open Food Facts** (solo datos no-precio).
- Definición flexible de comidas (`MealRequirement`): huecos permitidos, raciones variables, tuppers.
- Generación **asíncrona** de planes (job en Postgres) con estados y polling.
- Motor determinista: normalización, conversión de unidades, validación de alérgenos/dieta, cálculo de despensa,
  emparejamiento de productos, **optimización de envases**, nutrición, coste, planificación.
- Plan resultante con **cobertura de precios**, lista de compra **por categorías** con **modo offline** (IndexedDB).
- Regenerar una comida concreta; marcar favorito/rechazado.
- OpenAI **opcional** (BYOK): propone recetas candidatas dentro de un esquema estructurado.
- Modos de despliegue `self_hosted` / `cloud`; facturación IA `platform` / `byok` / `disabled` (sin pagos).

### 4.2 Fuera de alcance (MVP)

- Scraping de tiendas reales; integraciones oficiales de precios en producción.
- Adaptadores de cadenas activos (Aldi, Lidl, Carrefour, Dia, Alcampo, Deza): solo **esqueletos**.
  `MercadonaCommunityAdapter` existe pero está **experimental y desactivado**.
- Pagos, planes de suscripción, facturación monetaria.
- Optimización con **OR-Tools** (interfaz preparada, no implementada).
- **SSE** obligatorio, **Redis**, Kubernetes.
- App nativa (iOS/Android): el MVP es **PWA**.
- Consejo médico/nutricional profesional. Reconocimiento de tickets con OCR en producción
  (`user_receipt` existe como `source_type`, no como flujo pulido).

---

## 5. Las 22 pantallas iniciales

| # | Pantalla | Propósito |
|---|---|---|
| 1 | Registro | Alta con email + contraseña (Argon2id, sesión opaca) |
| 2 | Inicio de sesión | Login/logout, rate limiting |
| 3 | Recuperación de contraseña | Flujo preparado (reset por email) |
| 4 | Onboarding / bienvenida | Explica la promesa, disclaimer y consentimiento IA |
| 5 | Hogar: creación y datos | Crear/editar `Household` |
| 6 | Hogar: miembros e invitaciones | `HouseholdMember`, `HouseholdInvitation`, roles owner/editor/viewer |
| 7 | Perfil dietético | Objetivos nutricionales (`DietaryProfile`) |
| 8 | Alergias e intolerancias | `Allergy` — restricción **dura** |
| 9 | Restricciones y preferencias | `DietaryRestriction`, `FoodPreference` |
| 10 | Equipamiento de cocina | `Equipment` (horno, freidora de aire, tupper, etc.) |
| 11 | Selección de tienda | Cadena + provincia/localidad + CP + tienda + fecha de catálogo + cobertura |
| 12 | Despensa | `PantryItem`: qué hay disponible para descontar |
| 13 | Presupuesto y personas | Importe objetivo + nº de comensales |
| 14 | Definir comidas requeridas | `MealRequirement`: tipo, nº, raciones, tuppers, tiempo máx |
| 15 | Progreso de generación | Estado del job async (queued → … → completed/failed) con polling |
| 16 | Resultado del plan | Vista del `MealPlan` con **cobertura de precios** y coste conocido/estimado |
| 17 | Detalle de comida / receta | `PlannedMeal` + `Recipe` + ingredientes, pasos, envases |
| 18 | Regenerar una comida | Regenerar `PlannedMeal` concreto sin rehacer el plan |
| 19 | Sustitución de producto / precio manual | Reemplazar producto o introducir precio manual (`ProductSubstitution`) |
| 20 | Lista de compra por categorías | `GroceryList` agrupada; **offline** con IndexedDB |
| 21 | Sin solución / restricciones en conflicto | Explica conjunto mínimo de restricciones conflictivas y relajaciones posibles |
| 22 | Ajustes y privacidad | Exportar/eliminar cuenta, consentimiento OpenAI, desactivar IA, disclaimer |

---

## 6. Historias de usuario clave

- **US-01 — Plan con presupuesto**: *Como usuario, quiero indicar tienda, presupuesto, personas y comidas para
  recibir un plan y una lista de compra dentro de mi presupuesto, con el coste calculado por envases completos.*
- **US-02 — Alergia segura**: *Como usuario con alergia, quiero que ninguna receta ni producto propuesto contenga
  mi alérgeno, de forma garantizada por el motor y no por la IA.*
- **US-03 — Cobertura honesta**: *Como usuario, quiero ver claramente qué parte del coste es conocida y qué parte
  es estimada, y poder reemplazar un producto o meter un precio manual.*
- **US-04 — Comidas flexibles**: *Como usuario, quiero pedir solo las comidas que necesito (p. ej. 4 comidas y
  3 cenas, con tuppers para el trabajo) dejando huecos donde no cocino.*
- **US-05 — Regenerar una comida**: *Como usuario, quiero regenerar una única comida sin perder el resto del plan.*
- **US-06 — Favoritos / rechazos**: *Como usuario, quiero marcar recetas como favoritas o rechazadas para influir
  en futuros planes.*
- **US-07 — Reutilización de ingredientes**: *Como usuario, quiero que un ingrediente comprado para una receta se
  reutilice en otra (coste marginal) en lugar de comprarlo dos veces.*
- **US-08 — Offline en tienda**: *Como usuario, quiero consultar y tachar la lista de compra sin conexión estando
  en el supermercado.*
- **US-09 — Sin solución explicada**: *Como usuario, si mi presupuesto no da, quiero saber por qué (qué restricciones
  chocan) y qué puedo relajar, no un plan falso.*
- **US-10 — Hogar compartido**: *Como owner de un hogar, quiero invitar a otras personas con permisos de editor o
  viewer.*
- **US-11 — Privacidad e IA**: *Como usuario, quiero poder desactivar la IA y exportar/eliminar mis datos, y que
  nunca se envíen mis datos personales a OpenAI.*

---

## 7. Criterios de aceptación del MVP (sección 26 — 25 criterios)

> Reproducción como checklist. Cada criterio es verificable. El estado de implementación se rastrea en
> `docs/ROADMAP.md` (tabla de estado de los 25 criterios).

- [ ] **AC-01** — Un usuario puede registrarse con email + contraseña; la contraseña se almacena con **Argon2id**.
- [ ] **AC-02** — La sesión es **opaca en BD** (no JWT de larga duración en `localStorage`), cookie **HttpOnly**,
  `Secure` en producción, `SameSite` apropiado, con expiración y revocación.
- [ ] **AC-03** — Un usuario puede crear un **hogar** e invitar miembros con roles `owner`/`editor`/`viewer`;
  los permisos se respetan en las mutaciones.
- [ ] **AC-04** — El usuario define **alergias** y estas se aplican como **restricción dura**: ninguna receta,
  ingrediente ni producto propuesto en el plan final las viola.
- [ ] **AC-05** — El usuario define **restricciones dietéticas y preferencias**, aplicadas por el motor determinista.
- [ ] **AC-06** — El usuario selecciona una **tienda concreta** (cadena + provincia/localidad + CP + tienda +
  id interno + fecha de catálogo + cobertura).
- [ ] **AC-07** — El usuario define un **presupuesto** y un **nº de comensales** que condicionan el plan.
- [ ] **AC-08** — El usuario define **comidas requeridas flexibles** (`MealRequirement`): puede dejar huecos, variar
  raciones y pedir tuppers, sin obligación de llenar todas las comidas de todos los días.
- [ ] **AC-09** — La generación del plan es **asíncrona**: `POST /api/v1/plans/generate` devuelve **202** con
  `optimization_run_id` y `status_url`.
- [ ] **AC-10** — El job atraviesa estados observables: `queued → collecting_data → generating_candidates →
  validating → optimizing → completed` (o `failed`/`cancelled`), consultables por polling.
- [ ] **AC-11** — El cálculo de coste usa **envases completos**: si una receta necesita 600 g y el envase es de
  500 g, se compran **2 envases** (1000 g comprados, 600 consumidos, 400 sobrante). Nunca `600/500 × precio`.
- [ ] **AC-12** — Cada `ProductPrice` lleva **fuente + tienda + fecha** (`source_type`, `source_name`, `store_id`,
  `observed_at`). Nunca se inventa un precio ni se sustituye un ausente por `0`.
- [ ] **AC-13** — El plan muestra **cobertura de precios** (`price_coverage`, `weighted_price_coverage`) con estado
  (Completo / Cobertura alta / parcial / insuficiente / Datos caducados / Sin datos).
- [ ] **AC-14** — Si falta precio, el sistema distingue **"coste conocido"** de **"coste estimado"**, muestra rango
  y permite **reemplazar producto** o **introducir precio manual**.
- [ ] **AC-15** — El dinero se maneja como **`Decimal`/`numeric`** en el backend y como **string** en el frontend;
  no aparece ningún `float` en cálculos monetarios.
- [ ] **AC-16** — El motor determinista produce resultados **reproducibles** dada una **semilla** y el mismo contexto.
- [ ] **AC-17** — **Toda función crítica funciona sin OpenAI** (`AI_BILLING_MODE=disabled`): con recetas semilla el
  plan se genera igualmente.
- [ ] **AC-18** — Cuando OpenAI está activo, sus candidatos pasan el **flujo obligatorio de 12 pasos** y OpenAI
  **no** decide seguridad de alergia, precio, coste, nº de envases, disponibilidad ni conversión de unidades.
- [ ] **AC-19** — El contexto enviado a OpenAI está **pseudonimizado**: nunca incluye nombres reales, email ni
  identificadores internos.
- [ ] **AC-20** — Se genera una **lista de compra por categorías** funcional **offline** (IndexedDB): se puede
  consultar y tachar sin conexión.
- [ ] **AC-21** — El usuario puede **regenerar una única comida** sin rehacer el resto del plan.
- [ ] **AC-22** — El usuario puede marcar recetas como **favorito** o **rechazado**, influyendo en la puntuación.
- [ ] **AC-23** — Cuando **no hay solución**, el sistema **no** devuelve un plan falso: devuelve el conjunto mínimo
  de restricciones conflictivas, el presupuesto mínimo hallado, los productos que provocan el exceso y las
  restricciones blandas relajables.
- [ ] **AC-24** — El usuario puede **exportar y eliminar** su cuenta, **desactivar la IA** y dar **consentimiento
  específico** para OpenAI; los datos personales se borran/anonimizan de verdad.
- [ ] **AC-25** — Se muestra el **disclaimer** obligatorio: *"CestaPlan facilita la planificación y ofrece
  información orientativa. No sustituye el consejo de un profesional sanitario. Comprueba siempre las etiquetas de
  los productos en caso de alergia o intolerancia."*

---

## 8. Restricciones estrictas (sección 28)

Estas restricciones son **vinculantes** para todo el desarrollo y no admiten excepción sin decisión explícita del
propietario del proyecto.

### 8.1 Datos y precios

- **Nunca inventar precios.** Un precio sin fuente no existe.
- **Nunca sustituir un precio ausente por `0`.** La ausencia se propaga como "sin datos", no como coste cero.
- **No confundir** precio por kg/l con precio del **envase**.
- **No presentar estimaciones como reales** ni **mezclar tiendas** sin avisar.
- **No usar datos caducados** (`expires_at` pasado) como actuales.
- **Sin scraping** ni elusión de CAPTCHA/anti-bot. Conectores comunitarios **desactivados por defecto**.
- **Open Food Facts** solo para datos **no-precio** (código de barras, ingredientes, alérgenos, nutrición, categorías,
  marcas, imagen si la licencia lo permite), respetando **ODbL** (atribución + share-alike).

### 8.2 Seguridad de alergias e IA

- **Las alergias son restricción dura**, validadas por el motor (`AllergenValidator`), nunca por el LLM.
- **OpenAI no decide** definitivamente: seguridad de alergia, precio, coste total, nº de envases, disponibilidad,
  calorías/macros definitivos, cumplimiento de presupuesto, conversión de unidades ni la tienda de un precio.
- **El modelo no se hardcodea** en la lógica de negocio (`OPENAI_MODEL` por env).
- **Nada de texto libre fuera del esquema** estructurado de receta candidata.

### 8.3 Dinero y reproducibilidad

- **Dinero siempre exacto**: `Decimal`/`numeric`; en JS como **string**; **nunca `float`**.
- **Cálculo por envases completos**, con rastreo de sobrante, coste imputable y coste marginal.
- **Reproducibilidad**: semilla fija ⇒ mismo resultado; explicación **auditable** almacenada.

### 8.4 Privacidad y seguridad

- **Nunca enviar a OpenAI** nombres reales, email ni identificadores internos: pseudonimizar el contexto.
- **Datos sensibles** (alergias, objetivos, preferencias) minimizados; exportación y eliminación reales;
  consentimiento específico para IA; poder **desactivar la IA**.
- **CORS restrictivo**, cabeceras de seguridad, validación estricta, límites de tamaño, secretos seguros,
  **CSRF** en mutaciones, **rate limiting** en login, auditoría admin (`AuditLog`).
- **No es consejo médico**: disclaimer obligatorio siempre visible donde afecte a salud.

### 8.5 Alcance e infraestructura

- **Sin Redis, sin K8s, sin pagos** en el MVP. Cola de trabajos en **PostgreSQL** (`SELECT FOR UPDATE SKIP LOCKED`).
- **OR-Tools, SSE y Redis**: preparados pero **no activados**.
- **Historial de precios**: insertar filas nuevas, **no** `UPDATE` destructivo.
- **Código MIT**; **datos** con licencia separada y procedencia documentada.

---

## 9. Definición de "hecho" del MVP

El MVP se considera completo cuando los **25 criterios de aceptación** están verdes (ver tabla de estado en
`docs/ROADMAP.md`), el **vertical slice** de FASE 3 es ejecutable de principio a fin, y todas las **restricciones
estrictas** de §8 se cumplen sin excepción.
