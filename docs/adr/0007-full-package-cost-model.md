# 0007 — Coste por envases completos, no por fracción proporcional

- **Estado:** Aceptado
- **Fecha:** 2026-07-21
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

El presupuesto debe reflejar lo que el usuario realmente paga en caja. Si una receta necesita
600 g de pollo y la tienda vende bandejas de 500 g, el usuario compra **2 bandejas** (1000 g),
no 1,2 bandejas. Calcular el coste como `600/500 × precio_bandeja` subestima el gasto real y rompe
la promesa de que el presupuesto es una restricción real.

## Opciones consideradas

1. **Coste proporcional** (`cantidad_necesaria / cantidad_envase × precio`). Sencillo pero
   **irreal**: ignora que los productos se venden en envases indivisibles. Descartado.
2. **Redondeo hacia arriba a envases enteros por ingrediente aislado.** Correcto para un ingrediente,
   pero no aprovecha sobrantes reutilizables entre recetas ni la despensa.
3. **Optimizador de envases** que, por producto, parte de la cantidad necesaria total del plan,
   descuenta la despensa, calcula la cantidad pendiente y elige la combinación de envases que la
   cubre, rastreando sobrante, coste total, coste imputable y coste marginal.

## Decisión

Adoptamos la opción 3, implementada por `PackageOptimizer` (ver [`OPTIMIZATION.md`](../OPTIMIZATION.md)).
Para cada producto se rastrea: cantidad necesaria, disponible en despensa, pendiente, envases
seleccionados, cantidad comprada, cantidad utilizada, sobrante, coste total, coste imputable a la
receta y coste marginal si el producto ya se compra para otra receta. Toda la aritmética usa `Decimal`
(ver [ADR-0003](0003-decimal-money.md)). No se convierte "600/500 × precio". El plan reporta sobrantes
y coste por día/total con su diferencia respecto al presupuesto.

## Consecuencias

- **Positivas:** el coste refleja el ticket real; se visibilizan sobrantes (poco desperdicio) y se
  puede optimizar la reutilización de ingredientes entre recetas.
- **Negativas / coste asumido:** la selección de envases es una búsqueda discreta (combinaciones),
  más costosa que una división; se acota con búsqueda limitada y heurísticas.
- **Seguimiento:** si aparecen casos con muchos tamaños de envase por producto, evaluar la interfaz
  futura de OR-Tools para la selección de envases (preparada, no activada en el MVP).
