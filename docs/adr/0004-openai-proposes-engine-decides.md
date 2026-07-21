# 0004 — OpenAI propone; el motor determinista valida y calcula

- **Estado:** Aceptado
- **Fecha:** 2026-07-21
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

Un LLM es excelente proponiendo recetas y redactando texto, pero es no determinista y puede
alucinar. En CestaPlan hay decisiones que **no admiten error**: seguridad frente a alergias,
precios, coste total, número de envases, disponibilidad, macros definitivos, cumplimiento del
presupuesto y conversión de unidades. Además, toda función crítica debe poder ejecutarse sin
depender de OpenAI.

## Opciones consideradas

1. **El LLM lo decide todo** (recetas + cantidades + coste + validación). Máxima flexibilidad,
   inaceptable en fiabilidad, seguridad y auditabilidad.
2. **El LLM decide y el motor "revisa" opcionalmente.** El límite queda difuso; tarde o temprano
   una salida del LLM se cuela como verdad.
3. **Frontera dura: OpenAI propone candidatos estructurados; el motor determinista valida y
   calcula todo lo crítico.** El LLM es una fuente de *sugerencias*, nunca de *hechos* económicos
   ni de seguridad.

## Decisión

Adoptamos la opción 3. OpenAI se usa mediante la Responses API con salida estructurada por JSON
Schema (ver [`OPENAI.md`](../OPENAI.md)); el modelo se configura por variables de entorno y **no se
hardcodea** en la lógica de negocio. El flujo obligatorio de 12 pasos coloca al motor determinista
como única autoridad sobre restricciones duras, nutrientes, envases, coste y presupuesto. Si OpenAI
está desactivado (`AI_BILLING_MODE=disabled`) o no disponible, el sistema funciona con recetas
semilla existentes. Cada plan almacena una explicación auditable.

**OpenAI PUEDE:** proponer recetas, redactar instrucciones, clasificar estilos, sugerir sustituciones,
explicar la elección, crear variaciones, normalizar texto libre (sujeto a validación), proponer título/descripción.
**OpenAI NO PUEDE decidir:** seguridad de alergia, precio, coste total, nº de envases, disponibilidad,
calorías/macros definitivos, cumplimiento de presupuesto, conversión de unidades ni a qué tienda pertenece un precio.

## Consecuencias

- **Positivas:** fiabilidad, seguridad ante alergias, auditabilidad y reproducibilidad; el producto
  funciona sin OpenAI; el coste de IA es acotable y opcional.
- **Negativas / coste asumido:** más código determinista que mantener (normalización, validación,
  cálculo); las propuestas del LLM que no casan con el catálogo permitido se rechazan, lo que puede
  reducir la variedad si el catálogo es pobre.
- **Seguimiento:** vigilar la tasa de rechazo de candidatos; si es alta, mejorar el prompt/esquema
  y el mapeo de ingredientes antes que relajar la frontera.
