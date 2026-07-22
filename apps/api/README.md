# cestaplan-api

Backend de CestaPlan. Contiene tres paquetes Python:

- `cestaplan_api` — aplicación FastAPI (auth, hogares, tiendas, planes, lista de compra).
- `cestaplan_engine` — motor determinista (normalización, validación de alergias, envases, coste, optimización). Sin dependencias de web ni de OpenAI.
- `cestaplan_worker` — procesador de la cola de trabajos sobre PostgreSQL. Comparte código con `cestaplan_api`. En Railway se despliega como servicio `worker` (sin dominio) usando este mismo código.

Gestión con [uv](https://docs.astral.sh/uv/) y Python 3.12.

```bash
uv sync                                   # instala dependencias
uv run alembic upgrade head               # migraciones
uv run uvicorn cestaplan_api.main:app --reload
uv run python -m cestaplan_worker.main    # worker
uv run pytest                             # tests
```
