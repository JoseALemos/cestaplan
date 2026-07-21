# 0003 — Dinero con Decimal/numeric, nunca float

- **Estado:** Aceptado
- **Fecha:** 2026-07-21
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

El presupuesto es una restricción real y auditable. Sumar precios de envases, calcular costes
imputables y marginales, y comparar contra un presupuesto exige aritmética exacta. Los `float`
(IEEE-754) introducen errores de redondeo inaceptables en dinero (p. ej. `0.1 + 0.2 != 0.3`).

## Opciones consideradas

1. **float / double.** Rápido y nativo, pero inexacto para dinero. Descartado por principio.
2. **Enteros en céntimos.** Exacto, pero incómodo para precios unitarios por kg/litro con más
   decimales y propenso a errores de escala.
3. **Decimal (Python) / `numeric` (PostgreSQL) / string en la frontera JSON.** Exacto y explícito.

## Decisión

Todo importe monetario se representa con `decimal.Decimal` en Python y `numeric` en PostgreSQL.
En la frontera HTTP/JSON el dinero viaja como **string** (nunca `number` de JavaScript, que es
`double`), y Zod valida el formato decimal en el frontend. La aritmética monetaria usa una política
de redondeo explícita (`ROUND_HALF_UP`) y una escala documentada por moneda. El LLM **nunca** realiza
cálculos económicos (ver [ADR-0004](0004-openai-proposes-engine-decides.md)).

## Consecuencias

- **Positivas:** exactitud, reproducibilidad y auditabilidad del cálculo de coste; sin sorpresas de
  redondeo entre front y back.
- **Negativas / coste asumido:** hay que serializar/deserializar Decimal↔string con cuidado en Pydantic
  y en los contratos; ligeramente más verboso.
- **Seguimiento:** definir un tipo `Money` compartido en `packages/contracts` y helpers de redondeo
  centralizados para evitar que se cuele un `float` en algún cálculo.
