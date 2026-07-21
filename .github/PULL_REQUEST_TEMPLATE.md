## Descripción

Explica qué cambia este PR y por qué. Enlaza el issue relacionado (`Closes #…`).

## Tipo de cambio

- [ ] `fix` — corrección de bug
- [ ] `feat` — nueva funcionalidad
- [ ] `docs` — documentación
- [ ] `refactor` — refactor sin cambio de comportamiento
- [ ] `test` — tests
- [ ] `chore` — mantenimiento / tooling

## Checklist

Antes de solicitar revisión, confirma:

- [ ] `make lint` en verde (Ruff + ESLint).
- [ ] `make typecheck` en verde (Pyright + `tsc`).
- [ ] `make test` en verde (Pytest + Vitest/Playwright).
- [ ] **Documentación** actualizada (`docs/`, contratos y README si aplica).
- [ ] Commits **atómicos** y con mensajes claros.
- [ ] Diff **acotado** al alcance del PR (sin refactors "de paso").

## Principios innegociables

- [ ] **Sin scraping** ni elusión de CAPTCHA/anti-bot.
- [ ] **Sin precios inventados**: todo precio lleva fuente + tienda + fecha; un
      precio ausente es "sin dato", no `0`.
- [ ] **Dinero con `Decimal`** (Python) / `numeric` (Postgres) / **string** (JS).
      Ningún `float` para dinero.
- [ ] **Envases completos**: el coste se calcula por envases, no prorrateando.
- [ ] **Alergias** y cálculos económicos siguen decididos por el **motor
      determinista**, no por el LLM.
- [ ] Si toca IA: **no** se envían a OpenAI nombres reales, email ni identificadores
      internos (contexto pseudonimizado), y la función **degrada sin OpenAI**.

## Notas para quien revisa

Contexto adicional, decisiones de diseño, capturas o pasos de prueba manual.
