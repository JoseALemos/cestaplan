# Retención de datos de captura

Política de retención de las capturas crudas (`RawCapture`), redacción de secretos,
compresión, caducidad, limpieza y minimización de datos personales (§21 de la
especificación).

Código: `apps/api/src/cestaplan_api/ingestion/capture.py` (`RawCaptureRepository`).
Ver también: [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md),
[`PRICE_INGESTION.md`](PRICE_INGESTION.md), [`PRIVACY.md`](PRIVACY.md).

---

## 1. Qué es una `RawCapture`

Un snapshot **inmutable** de una respuesta de la fuente, guardado para
**reproducibilidad y re-parseo** (poder volver a extraer precios sin re-pedir a la
fuente). Convierte un `HttpFetchResult` en una fila almacenada aplicando las reglas
de seguridad del subsistema.

Campos relevantes: `source_url`, `response_status`, `content_type`,
`content_encoding`, `body_hash` (sha256), `response_headers` (**redactadas**),
`body_data` (opcional, comprimido), `is_block_page`, `retention_policy`, `expires_at`,
`captured_at`, `parser_version`.

---

## 2. Retención por resultado

La política de retención se elige **según el resultado del fetch**
(`retention_policy_for`). No todas las capturas valen lo mismo.

| Resultado | Etiqueta `retention_policy` | Se guarda el cuerpo | Por qué |
|-----------|-----------------------------|:-------------------:|---------|
| **Error** o página de bloqueo (`error is not None` o `is_block_page`) | `extended` | Sí | Es lo más útil para depurar; se conserva más tiempo. |
| **Cambiado** (respuesta nueva con cuerpo) | `medium` | Sí | La captura re-parseable; el caso normal útil. |
| **Sin cambios** (304 / hash idéntico) | `short` | **No** | El cuerpo idéntico ya está en disco; no se re-almacena. |

La distinción "sin cambios" se apoya en el Conditional GET del `HttpFetcher`
(`If-None-Match` / `If-Modified-Since`) y en el hash sha256 del cuerpo: si coincide
con la captura previa, `not_modified`/`from_cache` es `True` y **no** se vuelve a
guardar el body (`policy = short`).

---

## 3. Redacción de secretos (nunca se almacenan)

Antes de persistir, las cabeceras de respuesta se pasan por `redact_headers()`
(`ingestion/http_fetcher.py`). Se sustituye por `REDACTED` el valor de:

- `Authorization`, `Proxy-Authorization`
- `Cookie`, `Set-Cookie`
- `X-Api-Key`, `Api-Key`, `X-Auth-Token`, `X-Csrf-Token`
- y, defensivamente, cualquier cabecera cuyo nombre contenga `authorization`,
  `cookie`, `token`, `secret` o `api-key`.

Los valores crudos **nunca llegan a la base de datos**. Los logs de la cola aplican la
misma redacción a los payloads (`ingestion/queue.py`). El `Conditional GET` re-lee
`ETag` / `Last-Modified` desde las cabeceras ya redactadas — no necesita secretos.

---

## 4. Compresión

El cuerpo almacenado puede comprimirse con gzip (`compress=True` por defecto):
`body_data = gzip.compress(...)` y `content_encoding = "gzip"`. Reduce el espacio de
las capturas `medium`/`extended` sin perder reproducibilidad.

---

## 5. Caducidad y limpieza

Cada captura recibe `expires_at = captured_at + RAW_CAPTURE_RETENTION_DAYS` (por
defecto **30 días**, `expires_from_now`). La limpieza es explícita:

```python
RawCaptureRepository(session).cleanup_expired()  # borra filas con expires_at vencido
```

`cleanup_expired()` hace `DELETE` de las capturas cuyo `expires_at` ha pasado y
devuelve cuántas eliminó. Conviene ejecutarla periódicamente (p. ej. como paso de un
job de mantenimiento) para que el almacén de capturas no crezca indefinidamente.

| Parámetro | Valor por defecto | Variable |
|-----------|:-----------------:|----------|
| Horizonte de retención | 30 días | `RAW_CAPTURE_RETENTION_DAYS` |

> El historial de precios (`PriceObservation`) es **append-only** y tiene un ciclo de
> vida distinto: no caduca con la captura. La `RawCapture` es evidencia/reproducción;
> la observación es el dato de precio versionado. Borrar capturas caducadas no borra
> el historial de precios.

---

## 6. Minimización de datos personales

- Las capturas son de **catálogo y precios públicos**, no de datos personales de
  usuarios. No se capturan carritos, sesiones ni perfiles.
- Cookies y cabeceras de sesión se **redactan** (§3), así que no se persiste material
  que pudiera identificar a una persona o reutilizar una sesión.
- No se crean cuentas ni se accede a áreas autenticadas (ver
  [`SCRAPING_POLICY.md`](SCRAPING_POLICY.md)); por tanto no hay datos personales de
  terceros entrando por esta vía.
- El `body_hash` permite deduplicar y verificar integridad sin conservar más cuerpo
  del necesario (las respuestas `short` no re-almacenan body).

La política general de privacidad del proyecto está en [`PRIVACY.md`](PRIVACY.md).
