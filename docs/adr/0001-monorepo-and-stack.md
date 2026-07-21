# 0001 — Monorepo pnpm+Turborepo (JS) y uv+Python 3.12 (backend)

- **Estado:** Aceptado
- **Fecha:** 2026-07-21
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

CestaPlan tiene un frontend Next.js/TypeScript y un backend Python/FastAPI que comparten
contratos de datos, además de un worker que reutiliza el código del backend. Necesitamos:

- Una única fuente de verdad para los contratos front/back.
- Builds reproducibles y cacheables en CI.
- Que el backend no dependa del Python 3.8 del sistema (EOL, incompatible con el objetivo
  Pydantic v2 / SQLAlchemy 2 moderno).
- Que Railway pueda desplegar cada servicio con su propio *root directory*.

## Opciones consideradas

1. **Dos repos separados (front y back).** Simplifica el tooling por lenguaje pero complica
   la sincronización de contratos y el versionado conjunto; peor para un proyecto de un solo
   equipo pequeño.
2. **Monorepo con Nx.** Potente pero pesado y con curva de aprendizaje alta para contribuidores
   open source ocasionales.
3. **Monorepo pnpm workspaces + Turborepo para JS, y `uv` para el backend Python.** Ligero,
   estándar de facto en el ecosistema JS actual; `uv` gestiona una versión de Python aislada
   (3.12) sin tocar el sistema y es muy rápido.

## Decisión

Adoptamos la opción 3. El monorepo vive en `/root/cestaplan` con la estructura de la sección 14
del encargo. `pnpm-workspace.yaml` + `turbo.json` orquestan `apps/web` y `packages/*`. El backend
(`apps/api`, `apps/worker`) se gestiona con `uv` fijando **Python 3.12**. Los contratos viven en
`packages/contracts`: se derivan de los modelos Pydantic v2 a JSON Schema y de ahí se generan tipos
TypeScript y esquemas Zod, garantizando una única fuente de verdad.

## Consecuencias

- **Positivas:** un solo checkout, contratos sincronizados, builds cacheados, despliegue por
  servicio en Railway con *root directory* independiente. `uv` evita el problema del Python 3.8 del sistema.
- **Negativas / coste asumido:** el monorepo mezcla dos toolchains (JS y Python); los contribuidores
  deben tener Node 22+, pnpm y `uv`. Turborepo no cachea las tareas Python (se cachean vía `uv`/CI aparte).
- **Seguimiento:** si el número de paquetes JS crece mucho, revisar si Turborepo remote cache aporta.
