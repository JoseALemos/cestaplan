# OPENAI — Integración con OpenAI

> **Principio rector**: *OpenAI propone; el núcleo determinista valida y calcula.*
> OpenAI genera **recetas candidatas** y texto; **nunca** decide seguridad de alergias, dinero,
> envases, disponibilidad ni nutrición definitiva. Toda función crítica funciona **sin** OpenAI.

Este documento describe cómo CestaPlan integra OpenAI: **Responses API** con **salidas
estructuradas por JSON Schema**, configuración por entorno, el reparto de responsabilidades
(qué puede y qué no puede decidir la IA), el **flujo obligatorio de 12 pasos**, el **JSON
Schema completo** de una receta candidata, el manejo de errores y reintentos, el **fallback a
recetas semilla**, la **pseudonimización** del contexto y los **modos de facturación**.

---

## 1. Responses API + salidas estructuradas

CestaPlan usa la **Responses API** del SDK oficial de OpenAI con **structured outputs**
(`response_format` de tipo `json_schema`, `strict: true`). El modelo **debe** devolver JSON
que valide contra el esquema de receta candidata (§5). No se acepta texto libre fuera del
esquema.

### 1.1 Configuración por entorno

El modelo **NO se hardcodea** en la lógica de negocio. Todo es configurable:

| Variable | Propósito | Por defecto |
|---|---|---|
| `OPENAI_API_KEY` | Clave de API. Sólo se usa si `AI_BILLING_MODE != disabled`. | (vacío) |
| `OPENAI_MODEL` | Identificador del modelo. **No** se fija en código. | (vacío → obligatorio si IA activa) |
| `OPENAI_REASONING_EFFORT` | Esfuerzo de razonamiento (`low`/`medium`/`high`). | `medium` |
| `OPENAI_TIMEOUT_SECONDS` | Timeout por petición. | `60` |
| `OPENAI_MAX_RETRIES` | Reintentos con backoff ante fallos transitorios. | `2` |

Un `OpenAIClient` fino lee estas variables al construirse; ningún componente de negocio
menciona un modelo concreto. Cambiar de modelo es **sólo** cambiar `OPENAI_MODEL`.

```python
class OpenAISettings(BaseSettings):
    api_key: str | None = None
    model: str | None = None
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    timeout_seconds: int = 60
    max_retries: int = 2
    # prefijo de entorno: OPENAI_
```

---

## 2. Qué PUEDE y qué NO PUEDE decidir OpenAI

Copia fiel del canónico.

### 2.1 OpenAI **PUEDE**

- Proponer **recetas candidatas**.
- Redactar **instrucciones** de preparación.
- **Clasificar estilos** (cocina, etiquetas de preferencia).
- **Sugerir sustituciones** de ingredientes (como propuesta, sujeta a validación).
- **Explicar** la elección de una receta.
- Crear **variaciones** de una receta.
- **Normalizar texto libre** (sujeto a validación determinista posterior).
- Proponer **título** y **descripción**.

### 2.2 OpenAI **NO PUEDE** decidir definitivamente

- **Seguridad de alergia**.
- **Precio**.
- **Coste total**.
- **Número de envases**.
- **Disponibilidad** de un producto.
- **Calorías / macros** definitivos.
- **Cumplimiento de presupuesto**.
- **Conversión de unidades**.
- **Tienda** de un precio.

Todas estas decisiones pertenecen al **motor determinista** (ver `docs/OPTIMIZATION.md`).

---

## 3. Flujo obligatorio de 12 pasos

```mermaid
sequenceDiagram
    autonumber
    participant M as Motor determinista
    participant O as OpenAI (Responses API)
    participant V as Validadores
    participant C as Catalogo/Precios (tienda)
    participant Opt as PlanOptimizer
    participant DB as OptimizationRun (auditoria)

    M->>M: 1. Selecciona restricciones y contexto (pseudonimizado)
    M->>O: 2. Solicita candidatos (JSON Schema strict)
    O-->>M: candidatos estructurados
    M->>V: 3. Validar JSON contra esquema
    V->>V: 4. Normalizar ingredientes (IngredientNormalizer)
    V->>C: 5. Comparar con catalogo permitido (ProductMatcher)
    V->>V: 6. Validar restricciones DURAS (alergias + dieteticas)
    V->>V: 7. Calcular nutrientes (NutritionCalculator)
    V->>C: 8. Calcular envases completos (PackageOptimizer)
    V->>C: 9. Calcular coste (PriceCalculator)
    V->>V: 10. Rechazar incompatibles
    Opt->>Opt: 11. Elegir mejor combinacion (score + semilla)
    Opt->>DB: 12. Almacenar explicacion auditable
```

| Paso | Descripción |
|---|---|
| 1 | El **motor** selecciona las restricciones y el **contexto** a enviar (pseudonimizado, §7). |
| 2 | **OpenAI** devuelve **candidatos estructurados** conforme al JSON Schema. |
| 3 | **Validar el JSON** contra el esquema (rechazo si no valida). |
| 4 | **Normalizar ingredientes** (`IngredientNormalizer`, resolución de alias). |
| 5 | **Comparar con el catálogo permitido** de la tienda (`ProductMatcher`). |
| 6 | **Validar restricciones DURAS** (`AllergenValidator`, `DietaryRestrictionValidator`). |
| 7 | **Calcular nutrientes** (`NutritionCalculator`). |
| 8 | **Calcular envases** completos (`PackageOptimizer`). |
| 9 | **Calcular coste** (`PriceCalculator`: total/imputable/marginal). |
| 10 | **Rechazar** candidatos incompatibles (alergias, sin producto, fuera de presupuesto duro). |
| 11 | El **optimizador** elige la **mejor combinación** (score configurable + semilla reproducible). |
| 12 | **Almacenar la explicación auditable** (`OptimizationRun` + candidatos + restricciones). |

Los pasos 3–12 son **deterministas** y ocurren **siempre**, con IA o con recetas semilla.

---

## 4. Ubicación en la generación asíncrona

La llamada a OpenAI ocurre dentro del worker, durante los estados de la `GenerationJob`:

```text
POST /api/v1/plans/generate → 202 + optimization_run_id + status_url

queued → collecting_data → generating_candidates → validating → optimizing → completed
                                    (OpenAI, paso 2)    (pasos 3-10)  (paso 11)   (paso 12)
                                                                                  \→ failed / cancelled
```

El front hace **polling** con backoff sobre `status_url`. El job vive en Postgres
(`SELECT FOR UPDATE SKIP LOCKED`, reintentos limitados, backoff, heartbeat).

---

## 5. JSON Schema de la receta candidata

Esquema **completo** usado como `response_format` (`strict: true`). `additionalProperties:
false` en todos los objetos; `required` explícito. Nada de texto libre fuera del esquema.

```json
{
  "name": "candidate_recipe",
  "schema": {
    "type": "object",
    "additionalProperties": false,
    "required": [
      "title",
      "description",
      "servings",
      "meal_types",
      "cuisine",
      "preference_tags",
      "ingredients",
      "steps",
      "preparation_minutes",
      "cooking_minutes",
      "required_equipment",
      "leftover_reuse",
      "storage_instructions",
      "reheating_instructions"
    ],
    "properties": {
      "title": {
        "type": "string",
        "description": "Título breve de la receta."
      },
      "description": {
        "type": "string",
        "description": "Descripción corta orientativa."
      },
      "servings": {
        "type": "integer",
        "minimum": 1,
        "description": "Número de raciones que produce la receta."
      },
      "meal_types": {
        "type": "array",
        "items": {
          "type": "string",
          "enum": ["breakfast", "lunch", "snack", "dinner"]
        },
        "minItems": 1,
        "description": "Tipos de comida a los que aplica."
      },
      "cuisine": {
        "type": "string",
        "description": "Estilo/cocina (p. ej. mediterránea, mexicana)."
      },
      "preference_tags": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Etiquetas de preferencia (p. ej. alta_proteina, rapida, economica)."
      },
      "ingredients": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "canonical_name",
            "display_name",
            "quantity",
            "unit",
            "optional",
            "substitution_group"
          ],
          "properties": {
            "canonical_name": {
              "type": "string",
              "description": "Nombre canónico en inglés para mapear al catálogo interno."
            },
            "display_name": {
              "type": "string",
              "description": "Nombre mostrado al usuario (idioma del usuario)."
            },
            "quantity": {
              "type": "number",
              "exclusiveMinimum": 0,
              "description": "Cantidad numérica (se convierte a Decimal en el motor)."
            },
            "unit": {
              "type": "string",
              "description": "Unidad de la cantidad (g, kg, ml, l, ud, etc.)."
            },
            "optional": {
              "type": "boolean",
              "description": "Si el ingrediente es opcional."
            },
            "substitution_group": {
              "type": ["string", "null"],
              "description": "Grupo de sustitución; ingredientes intercambiables comparten grupo. null si no aplica."
            }
          }
        }
      },
      "steps": {
        "type": "array",
        "minItems": 1,
        "items": { "type": "string" },
        "description": "Pasos de preparación en orden."
      },
      "preparation_minutes": {
        "type": "integer",
        "minimum": 0,
        "description": "Minutos de preparación (sin cocción)."
      },
      "cooking_minutes": {
        "type": "integer",
        "minimum": 0,
        "description": "Minutos de cocción."
      },
      "required_equipment": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Equipamiento necesario (p. ej. horno, sartén, batidora)."
      },
      "leftover_reuse": {
        "type": ["string", "null"],
        "description": "Cómo reutilizar sobras / cocinar para varios días. null si no aplica."
      },
      "storage_instructions": {
        "type": ["string", "null"],
        "description": "Instrucciones de conservación. null si no aplica."
      },
      "reheating_instructions": {
        "type": ["string", "null"],
        "description": "Instrucciones de recalentado. null si no aplica."
      }
    }
  },
  "strict": true
}
```

> Nota: `quantity` viaja como número en el JSON del modelo y se convierte inmediatamente a
> `Decimal` en el `IngredientNormalizer` (paso 4). El dinero **nunca** aparece en este esquema:
> precio, coste y envases son competencia exclusiva del motor determinista.

---

## 6. Manejo de errores y resiliencia

Toda función crítica funciona **sin** OpenAI. Los fallos de IA **degradan con elegancia**, no
rompen la generación.

### 6.1 Respuesta inválida

Si el JSON no valida contra el esquema (§5) o falla la normalización básica:

1. Se descarta el candidato inválido (no se intenta "arreglar" con heurísticas).
2. Si quedan otros candidatos válidos, se continúa el flujo con ellos.
3. Si **ningún** candidato es válido, se hace **un reintento acotado** de la llamada; si
   persiste, se cae a **recetas semilla** (§6.4).
4. El evento se registra en `AuditLog` / `last_error` del job (sin volcar recetas privadas
   completas si no hace falta).

### 6.2 OpenAI no disponible

Si el servicio no responde, devuelve 5xx persistente, o `AI_BILLING_MODE=disabled`:

- La generación **no falla**: usa **recetas semilla** del catálogo interno (§6.4).
- El plan se marca con `source` = semilla para transparencia.
- El motor determinista (validación, envases, coste, optimización) se ejecuta igual.

### 6.3 Timeouts y reintentos con backoff

- Cada petición respeta `OPENAI_TIMEOUT_SECONDS`.
- Ante fallos **transitorios** (timeout, 429, 5xx) se reintenta hasta `OPENAI_MAX_RETRIES`
  veces con **backoff exponencial + jitter**.
- Errores **no recuperables** (4xx de esquema/entrada, clave inválida) **no** se reintentan.
- Agotados los reintentos, se aplica el fallback a semilla (§6.4). El `GenerationJob` sólo
  pasa a `failed` si tampoco hay semilla utilizable para cubrir los requisitos.

```text
intento i → espera = min(cap, base · 2^i) + jitter_aleatorio
```

### 6.4 Fallback a recetas semilla (`AI_BILLING_MODE=disabled`)

CestaPlan incluye **recetas semilla** deterministas. Cuando la IA está desactivada o no
disponible, el motor selecciona candidatos del banco de recetas semilla en lugar de pedirlos
a OpenAI, y ejecuta **exactamente los mismos pasos 3–12**. Esto garantiza que la planificación
—reproducible y auditable— **nunca depende** de OpenAI.

---

## 7. Pseudonimización del contexto

**NUNCA** se envían a OpenAI datos personales identificables. El motor (paso 1) construye un
contexto **pseudonimizado** antes de llamar.

### 7.1 Qué SÍ se envía

- Restricciones **duras** en forma abstracta (p. ej. "sin gluten", "alergia a frutos secos")
  — como categorías, no ligadas a una persona nombrada.
- Objetivos/etiquetas de preferencia (alta proteína, rápido, económico, veg/vegano).
- `MealRequirement` abstractos (tipos de comida, nº de raciones, tiempo máximo, tupper).
- Equipamiento disponible (categorías).
- Estilos de cocina deseados.

### 7.2 Qué NUNCA se envía

- **Nombres reales** (usuario, miembros del hogar).
- **Email** ni datos de contacto.
- **Identificadores internos** (UUID/PK de `User`, `Household`, `Store`, etc.).
- Direcciones, geolocalización precisa, ni datos de la tienda concreta más allá de lo
  necesario para el estilo de recetas (los **precios y la tienda** los resuelve el motor).
- Historial completo o cualquier dato sensible no imprescindible para proponer recetas.

Las alergias, objetivos nutricionales y preferencias son **datos sensibles de aplicación**:
se aplica minimización, requieren **consentimiento específico** para OpenAI y el usuario puede
**desactivar la IA**. Los logs no vuelcan recetas privadas completas si no hace falta.

---

## 8. UsageLedger y modos de facturación

`AI_BILLING_MODE` gobierna cómo se provee y contabiliza el uso de OpenAI.

| Modo | Clave de API | Contabilización | Uso |
|---|---|---|---|
| `platform` | Gestionada por el **servidor** (cloud). No se revela al usuario. | **`UsageLedger`** registra consumo; se aplican **cuotas**. Sin pagos aún. | Despliegue cloud multiusuario. |
| `byok` | El **usuario/admin** aporta `OPENAI_API_KEY` (bring your own key). | Consumo contra la clave del usuario; el `UsageLedger` puede registrar uso informativo. | Self-hosted o cloud con clave propia. Decisión del proyecto: **BYOK primero**. |
| `disabled` | Ninguna. | Sin consumo. | IA apagada; **sólo recetas semilla** (§6.4). Todo lo crítico sigue funcionando. |

### 8.1 `UsageLedger`

Registra por evento: hogar/usuario (interno, nunca enviado a OpenAI), `optimization_run_id`,
modelo usado, tokens/peticiones, timestamp y coste imputado cuando aplique. En modo `platform`
sirve para **cuotas** y transparencia; nunca revela la clave del servidor.

### 8.2 Relación con `DEPLOYMENT_MODE`

- `self_hosted`: el admin pone `OPENAI_API_KEY` (típicamente `byok`), sin límites por defecto,
  puede **desactivar la IA** (`disabled`) e importar catálogos.
- `cloud`: clave gestionada por el servidor (`platform`), registra consumo (`UsageLedger`),
  aplica cuotas y **no revela** la clave. Sin pagos aún.

---

## 9. Resumen de garantías

- El modelo **no está hardcodeado**: se configura por `OPENAI_MODEL`.
- OpenAI **propone**; el motor determinista **valida y calcula** (seguridad, dinero, envases,
  nutrición, disponibilidad, tienda).
- Salida **estructurada** obligatoria (JSON Schema `strict`, `additionalProperties: false`).
- Degradación elegante: respuesta inválida, servicio caído, timeouts/reintentos → **fallback
  a recetas semilla**.
- **Pseudonimización** estricta: nunca nombres reales, email ni ids internos.
- Consentimiento específico y posibilidad de **desactivar la IA**; consumo auditable vía
  `UsageLedger`.
