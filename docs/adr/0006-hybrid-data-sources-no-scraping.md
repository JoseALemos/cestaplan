# 0006 — Fuentes híbridas sin scraping en el MVP

- **Estado:** Aceptado
- **Fecha:** 2026-07-21
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

CestaPlan promete precios con fuente, tienda y fecha, y no inventarlos nunca. Ninguna cadena de
supermercados ofrece una API pública y legal de precios por tienda concreta. Necesitamos una
estrategia de datos que sea legal, honesta sobre su cobertura y viable en autohospedaje, sin
depender de técnicas prohibidas.

## Opciones consideradas

1. **Scraping de webs de supermercados.** Frágil, frecuentemente contrario a los términos de uso,
   y el encargo lo prohíbe explícitamente en el MVP (incluye no eludir CAPTCHA/anti-bot).
2. **Depender de una única API comercial.** No existe de forma abierta y acoplaría el producto.
3. **Arquitectura híbrida de adaptadores** con tipos de fuente explícitos y un contrato común
   `RetailerAdapter`, priorizando datos aportados por el usuario/instancia y datasets abiertos.

## Decisión

Adoptamos la opción 3. Definimos 9 `source_type` (`official`, `authorized_partner`,
`community_connector`, `open_dataset`, `admin_import`, `manual_entry`, `user_receipt`, `estimated`,
`demo`). En el MVP están **activos**: `DemoRetailerAdapter`, `Csv`, `Json`, `Manual` y
`OpenFoodFactsAdapter`. Open Food Facts se usa **solo** para código de barras, ingredientes,
alérgenos, nutrición, imagen (según licencia) y categorías/marcas — **nunca como fuente principal de
precios**. `MercadonaCommunityAdapter` se incluye como conector **experimental desactivado**; los
adaptadores de Aldi, Lidl, Carrefour, Dia, Alcampo y Deza son **esqueletos** que cumplen el contrato
sin datos. Todo conector comunitario es desactivable por flag y viene desactivado por defecto.
**No se hace scraping ni se eluden mecanismos anti-bot.** Cada precio guarda su procedencia completa
y su `confidence_score`/`verification_status`; las estimaciones nunca se presentan como reales.

## Consecuencias

- **Positivas:** legalidad y honestidad de datos; autohospedaje viable con catálogos propios;
  cobertura transparente; extensible por la comunidad vía adaptadores.
- **Negativas / coste asumido:** el MVP no es un comparador de precios "listo para usar": la utilidad
  de precios depende de que la instancia aporte datos (demo, CSV, manual, recibos).
- **Seguimiento:** cualquier conector nuevo pasa por revisión de licencia y términos; los datasets se
  documentan en [`DATA_SOURCES.md`](../DATA_SOURCES.md) con su licencia separada del MIT del código.
