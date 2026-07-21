# Guía para contribuir recetas

Esta guía explica cómo aportar recetas a CestaPlan de forma que el **motor
determinista** pueda validarlas, calcular sus envases y su coste, y respetar las
alergias como restricción dura.

> Recuerda: **OpenAI propone; el núcleo determinista valida y calcula.** Una receta
> aportada por la comunidad entra como dato estructurado, no como texto libre.

## Principios de una receta correcta

- **Estructurada, no texto libre.** Toda receta se ajusta al esquema; nada de
  campos improvisados.
- **Unidades canónicas.** Las cantidades usan unidades canónicas para que el
  `UnitConverter` pueda operar (ver más abajo).
- **Alérgenos declarados.** Los alérgenos son información de seguridad; se declaran
  explícitamente por ingrediente y a nivel de receta.
- **Sustituciones agrupadas.** Los ingredientes intercambiables comparten un
  `substitution_group`.
- **`is_synthetic`.** Las recetas de demo/semilla sintéticas se marcan con
  `is_synthetic=true` y **nunca** se presentan como reales.

## Esquema de la receta

Campos (nombres de campo en inglés; los valores de texto pueden ir en español):

| Campo | Tipo | Notas |
|-------|------|-------|
| `title` | string | Título de la receta. |
| `description` | string | Descripción breve. |
| `servings` | int | Raciones base. |
| `meal_types` | string[] | Subconjunto de: `breakfast`, `lunch`, `snack`, `dinner`. |
| `cuisine` | string | Estilo de cocina. |
| `preference_tags` | string[] | P. ej. `high_protein`, `vegan`, `vegetarian`, `quick`, `family`, `low_cal`. |
| `ingredients` | Ingredient[] | Ver esquema de ingrediente. |
| `steps` | RecipeStep[] | Pasos ordenados. |
| `preparation_minutes` | int | Tiempo de preparación. |
| `cooking_minutes` | int | Tiempo de cocción. |
| `required_equipment` | string[] | Equipamiento necesario. |
| `leftover_reuse` | string | Cómo reutilizar sobras. |
| `storage_instructions` | string | Conservación. |
| `reheating_instructions` | string | Recalentado. |
| `is_synthetic` | bool | `true` para recetas demo/semilla sintéticas. |

### Esquema del ingrediente

| Campo | Tipo | Notas |
|-------|------|-------|
| `canonical_name` | string | Nombre canónico (inglés) que resuelve el `IngredientNormalizer`. |
| `display_name` | string | Nombre mostrado al usuario (puede ir en español). |
| `quantity` | number | Cantidad en unidad canónica. |
| `unit` | string | Unidad canónica (ver tabla). |
| `optional` | bool | Si el ingrediente es opcional. |
| `substitution_group` | string \| null | Agrupa ingredientes intercambiables. |

### Ejemplo (YAML ilustrativo)

```yaml
title: "Salteado de pollo y verduras con arroz"
description: "Alto en proteína y listo en menos de 30 minutos."
servings: 2
meal_types: [lunch, dinner]
cuisine: "asian"
preference_tags: [high_protein, quick]
preparation_minutes: 10
cooking_minutes: 15
required_equipment: [wok, stove]
ingredients:
  - canonical_name: chicken_breast
    display_name: "Pechuga de pollo"
    quantity: 300
    unit: g
    optional: false
    substitution_group: main_protein
  - canonical_name: tofu_firm
    display_name: "Tofu firme"
    quantity: 300
    unit: g
    optional: false
    substitution_group: main_protein   # sustituto vegano del pollo
  - canonical_name: white_rice
    display_name: "Arroz blanco"
    quantity: 160
    unit: g
    optional: false
    substitution_group: null
steps:
  - "Cuece el arroz según el paquete."
  - "Saltea la proteína en el wok a fuego fuerte."
  - "Añade las verduras y sirve sobre el arroz."
leftover_reuse: "Ideal como tupper para el día siguiente."
storage_instructions: "Frigorífico hasta 2 días en recipiente hermético."
reheating_instructions: "Microondas 2-3 min o sartén caliente."
is_synthetic: true
```

## Unidades canónicas

Usa unidades que el `UnitConverter` entienda. Recomendadas:

- **Masa:** `g` (gramo). Deriva `kg` internamente.
- **Volumen:** `ml` (mililitro). Deriva `l` internamente.
- **Unidades discretas:** `unit` (piezas: p. ej. huevos, latas).

Evita medidas ambiguas ("una taza", "un puñado") en el dato estructurado; si el
texto de un paso las menciona, la cantidad real va en la unidad canónica del
ingrediente. Nunca confundas cantidad de **receta** con cantidad de **envase**: el
`PackageOptimizer` se encarga de traducir necesidad → envases completos.

## Alérgenos

- Declara los alérgenos relevantes a nivel de ingrediente y de receta.
- El **`AllergenValidator`** (determinista) es quien decide la seguridad; una receta
  con un alérgeno declarado será filtrada como **restricción dura** para los
  hogares afectados.
- Ante la duda, **declara el alérgeno**. Nunca lo omitas para "que encaje" en más
  planes.

## Sustituciones

- Los ingredientes intercambiables comparten `substitution_group` (p. ej.
  `main_protein`).
- Una sustitución no debe romper una restricción dura: p. ej. un sustituto vegano
  debe estar libre de los alérgenos/ingredientes que la variante excluye.
- El motor puede elegir dentro del grupo según disponibilidad, coste y despensa.

## Cómo se validan las recetas

Al aportar una receta se comprueba:

1. **Conformidad de esquema** (todos los campos requeridos, tipos correctos).
2. **Nombres canónicos resolubles** por el `IngredientNormalizer`.
3. **Unidades convertibles** por el `UnitConverter`.
4. **Alérgenos coherentes** con los ingredientes declarados.
5. **Grupos de sustitución** válidos y sin conflictos de restricción dura.
6. **Marcado `is_synthetic`** correcto para recetas demo/semilla.

Ejecuta `make lint`, `make typecheck` y `make test` antes de abrir el PR. Las
recetas semilla van en `data/demo` (con `is_synthetic=true`).

## Datos demo

El conjunto demo incluye recetas **sintéticas** (objetivo aproximado: 50 recetas
sobre un supermercado ficticio, con opciones veg/vegana/alta proteína/rápida/
familiar/baja caloría). Todas con `is_synthetic=true` y **nunca** presentadas como
reales.

Ver también: [ADAPTER_GUIDE.md](./ADAPTER_GUIDE.md) ·
[PRICE_SOURCES_GUIDE.md](./PRICE_SOURCES_GUIDE.md) ·
[/CONTRIBUTING.md](../CONTRIBUTING.md).
