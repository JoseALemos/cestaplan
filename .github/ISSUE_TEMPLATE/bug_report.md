---
name: "🐛 Informe de bug"
about: "Reporta un comportamiento incorrecto en CestaPlan"
title: "[bug]: "
labels: ["bug"]
assignees: []
---

## Descripción

Describe con claridad qué falla.

## Pasos para reproducir

1. Ir a '...'
2. Hacer clic en '...'
3. Observar el error

## Comportamiento esperado

Qué debería haber ocurrido.

## Comportamiento real

Qué ocurrió realmente. Incluye mensajes de error o capturas si ayudan.

## Área afectada

- [ ] `apps/web` (PWA / frontend)
- [ ] `apps/api` (FastAPI / motor determinista / OpenAI)
- [ ] `apps/worker` (cola de trabajos)
- [ ] `packages/contracts` (contratos)
- [ ] Datos / importación
- [ ] Documentación
- [ ] Otro

## ¿Afecta a un cálculo sensible?

- [ ] Cálculo de precio / presupuesto / envases
- [ ] Validación de alergias o restricciones dietéticas
- [ ] Cobertura de precios
- [ ] No aplica

## Entorno

- Modo de despliegue: `self_hosted` | `cloud`
- `AI_BILLING_MODE`: `platform` | `byok` | `disabled`
- Navegador / dispositivo (si es la PWA):
- Versión / commit:
- SO:

## Contexto adicional

Cualquier otra información relevante. **No incluyas secretos** (claves de API,
`SESSION_SECRET`, cadenas de conexión reales) ni datos personales.
