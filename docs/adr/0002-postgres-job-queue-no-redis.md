# 0002 — Cola de trabajos sobre PostgreSQL sin Redis en el MVP

- **Estado:** Aceptado
- **Fecha:** 2026-07-21
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

La generación de un plan es cara y no determinista en latencia (llama a OpenAI, valida,
optimiza). No queremos mantener una petición HTTP abierta durante toda la generación:
`POST /api/v1/plans/generate` responde `202` con un `optimization_run_id` y un `status_url`,
y un worker procesa el trabajo en segundo plano. Necesitamos una cola con reintentos, backoff,
heartbeat y locking, pero queremos minimizar la superficie operativa del MVP.

## Opciones consideradas

1. **Redis + RQ/Celery.** Estándar y potente, pero añade un servicio más que operar, desplegar
   y monitorizar en Railway, y una dependencia de infraestructura que el encargo pide evitar
   salvo necesidad demostrada.
2. **Cola gestionada externa (SQS, etc.).** Acopla el proyecto a un proveedor y complica el
   autohospedaje.
3. **Cola sobre PostgreSQL** con `SELECT ... FOR UPDATE SKIP LOCKED`. Postgres ya es una
   dependencia obligatoria; una tabla `GenerationJob` con locking pesimista cubre el MVP.

## Decisión

Implementamos la cola sobre PostgreSQL (opción 3). La tabla `GenerationJob` incluye `status`,
`attempts`, `max_attempts`, `locked_at`, `locked_by`, `last_error`, `run_after` (backoff) y
`heartbeat_at`. El worker hace polling con `SELECT FOR UPDATE SKIP LOCKED`, procesa, actualiza
heartbeat y libera. El frontend hace polling del `status_url` con backoff. Dejamos preparada una
interfaz para SSE, pero no es obligatoria en el MVP. **No se introduce Redis.**

## Consecuencias

- **Positivas:** una dependencia menos; autohospedaje trivial; transaccionalidad con el resto
  del modelo de datos; visibilidad de la cola vía SQL.
- **Negativas / coste asumido:** menor throughput que un broker dedicado; el polling añade carga
  ligera a Postgres. Aceptable para el volumen del MVP.
- **Seguimiento:** si el volumen de jobs o la latencia de polling se vuelven un problema medible,
  reconsiderar Redis/PgBouncer o `LISTEN/NOTIFY` + SSE. La interfaz del worker se diseña para permitir
  ese cambio sin tocar la lógica de negocio.
