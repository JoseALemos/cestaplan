---
name: "🛒 Propuesta de adaptador de supermercado"
about: "Propón un nuevo RetailerAdapter (conector de datos de una cadena)"
title: "[adapter]: "
labels: ["adapter", "needs-triage"]
assignees: []
---

Antes de implementar, propón el adaptador aquí. Lee primero
[docs/ADAPTER_GUIDE.md](../../docs/ADAPTER_GUIDE.md).

## Cadena / supermercado

Nombre de la cadena y ámbito geográfico (país, provincias, cobertura de tiendas).

> Supermercados ya contemplados por el modelo: Mercadona, Aldi, Lidl, Carrefour,
> Dia, Alcampo, Deza. Indica si es uno de estos o uno nuevo.

## Tipo de adaptador

- [ ] `official` — feed/API oficial
- [ ] `authorized_partner` — socio autorizado
- [ ] `community_connector` — conector comunitario (**desactivado por defecto**)
- [ ] `open_dataset` — dataset abierto
- [ ] `admin_import` — importación CSV/JSON (no requiere adaptador de red)

## Origen de los datos

¿De dónde saldrían los datos? Describe la fuente legítima.

## Cumplimiento (obligatorio)

- [ ] **Sin scraping** ni elusión de CAPTCHA/anti-bot.
- [ ] La fuente **permite** el uso previsto (enlaza términos/licencia).
- [ ] Si es `community_connector`, irá **desactivado por defecto**.
- [ ] Los precios llevarán **fuente + tienda + fecha**; nada de precios inventados.
- [ ] Respeta las licencias de datos (p. ej. **ODbL** para Open Food Facts).

## Datos que aportaría

- [ ] Precios (`ProductPrice`)
- [ ] Catálogo de productos / variantes
- [ ] Códigos de barras
- [ ] Alérgenos / ingredientes declarados
- [ ] Nutrición
- [ ] Categorías / marcas
- [ ] Disponibilidad

## Selección de tienda

¿Cómo se identifica una tienda concreta? (cadena + provincia/localidad + código
postal + tienda + id interno + fecha de actualización del catálogo + cobertura).

## Notas de implementación

Autenticación, límites de la fuente, frecuencia de actualización, riesgos y
cualquier detalle relevante para el contrato `RetailerAdapter`.
