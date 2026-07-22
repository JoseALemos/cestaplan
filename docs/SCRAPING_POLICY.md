# Política de acceso a fuentes

Reglas estrictas que gobiernan cómo el subsistema de ingesta accede (y **no** accede)
a las fuentes de precios (§2 de la especificación). Estas reglas son vinculantes: un
conector que no las cumpla no se activa.

Ver también: [`CONNECTOR_ARCHITECTURE.md`](CONNECTOR_ARCHITECTURE.md) (dónde se
aplican en el código), [`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md)
(estado por supermercado), ADR
[`0006`](adr/0006-hybrid-data-sources-no-scraping.md) y
[`0008`](adr/0008-price-ingestion-subsystem.md).

---

## 1. Principio rector

CestaPlan promete precios **con fuente, tienda y fecha, y no inventarlos nunca**. La
ingesta se hace **sólo desde fuentes legales y públicas**, con procedencia completa.
Ante la duda, no se ingiere.

---

## 2. Prohibido (sin excepciones)

| Prohibición | Por qué |
|-------------|---------|
| **Eludir CAPTCHA, anti-bot o muros de login.** | El acceso está deliberadamente restringido; sortearlo es abusivo y probablemente ilegal. El `HttpFetcher` **detecta y reporta** estas páginas, y **nunca** intenta resolverlas. |
| **Eludir bloqueos** (403/429, interstitials tipo Cloudflare "Just a moment"). | Un bloqueo es una señal de "para". Se marca `temporarily_blocked` y se detiene. |
| **Usar proxies/rotación de IP para ocultarse** o repartir carga y esquivar límites. | Ocultar el origen contradice el acceso honesto e identificable. |
| **Crear cuentas falsas** o usar credenciales para acceder a datos tras un login. | Acceso no autorizado. |
| **Tratar `robots.txt` como autorización.** | Que una ruta esté "permitida" en `robots.txt` **no** habilita su ingesta; hay que revisar además los términos de uso. Y un `Disallow` sobre los endpoints de datos es motivo directo de `permission_required`. |
| **Crawling agresivo.** | Ráfagas o alta concurrencia dañan la fuente. Se aplican límites conservadores (§4). |
| **Fabricar o rellenar datos.** | Nunca se inventa un precio, ni se pone `0` por un dato ausente, ni se presenta una estimación como real. Ver [`PRICE_QUALITY.md`](PRICE_QUALITY.md). |

**Regla de oro operativa:** *cuando una fuente está bloqueada o requiere
autenticación, se detiene y se marca `permission_required` (o `temporarily_blocked`);
no se evade.*

---

## 3. Orden de preferencia de fuentes

Se usa siempre la fuente **menos intrusiva y más estable** disponible. Las técnicas
más frágiles/costosas son último recurso, nunca el primero.

1. **Feed oficial** del retailer (si lo ofrece y su licencia lo permite).
2. **API pública** documentada y permitida.
3. **JSON-LD** incrustado en la página (datos estructurados públicos).
4. **JSON embebido** en el HTML (estado inicial de la página).
5. **HTML público** (parseo del marcado servido).
6. **JSON del navegador** (peticiones XHR/fetch que hace la propia web, si su acceso
   está permitido).
7. **Playwright sólo para contexto** (renderizado headless) — **no** para eludir
   anti-bot; sólo cuando el contenido legítimo requiere JS para mostrarse.
8. **PDF** (folletos de ofertas públicos).
9. **OCR** — **último recurso**, cuando no hay ninguna alternativa estructurada.

Bajar por esta lista requiere justificación; ninguna etapa habilita saltarse las
prohibiciones del §2.

---

## 4. Límites conservadores de rate

Aplicados por el `HttpFetcher` (`ingestion/http_fetcher.py`) y configurables por
`SCRAPING_*` (ver [`PRICE_INGESTION.md`](PRICE_INGESTION.md#4-variables-de-entorno)).

| Límite | Valor por defecto | Variable |
|--------|:-----------------:|----------|
| Concurrencia por dominio | **2** peticiones en vuelo | `SCRAPING_MAX_CONCURRENCY` |
| Retardo entre peticiones (mismo dominio) | **500–1500 ms** + jitter | `SCRAPING_REQUEST_DELAY_MIN_MS` / `_MAX_MS` |
| Reintentos | **3**, con backoff exponencial + jitter | `SCRAPING_MAX_RETRIES` |
| Timeout | **20 s** por petición | `SCRAPING_TIMEOUT_SECONDS` |
| Tamaño máximo de respuesta | **5 MB** (aborta por encima) | `SCRAPING_MAX_RESPONSE_MB` |
| User-Agent | Honesto e identificable (+ `From`) | `SCRAPING_USER_AGENT` / `SCRAPING_CONTACT_EMAIL` |

Además: **lista blanca de dominios** por conector (`SourcePolicy.allowed_domains`),
**guardia SSRF** (rechaza IPs privadas/loopback/link-local y esquemas no http(s)),
**Conditional GET** (ETag / If-Modified-Since + hash de cuerpo) para no re-descargar
lo que no cambió, y **circuit breaker** por dominio.

---

## 5. Detener y marcar `permission_required`

Cuando un conector encuentra que su fuente:

- prohíbe el acceso a sus endpoints de datos en `robots.txt`, o
- devuelve una página de bloqueo/CAPTCHA/login, o
- exige autenticación para acceder a los datos,

el subsistema **no** intenta continuar. La observación derivada de esa respuesta se
enruta a **quarantine** (validación `BLOCK_PAGE`), el `ConnectorState` pasa a
`permission_required` (bloqueo estructural) o `temporarily_blocked` (bloqueo
transitorio con `circuit_open_until`), y el scheduler deja de programar ese retailer.
Se registra la evaluación en `SourceAuditService` (`GET /api/v1/admin/sources`).

Esto es exactamente lo que ocurre hoy con **Mercadona**, **Carrefour** y **Lidl**: su
`robots.txt` prohíbe los endpoints de datos, así que sus conectores están presentes
como framework pero **desactivados y detenidos**, y **nunca** se rastrean. Activar su
flag opt-in no los pone a rastrear mientras la política los mantenga detenidos. Ver
[`RETAILER_SOURCE_MATRIX.md`](RETAILER_SOURCE_MATRIX.md).

---

## 6. Todo opt-in, desactivado por defecto

Ningún conector real accede a la red sin configuración explícita del operador:

- `SCRAPING_ENABLED=false` por defecto (interruptor maestro).
- Cada conector tiene su flag `*_CONNECTOR_ENABLED=false` por defecto.
- El único conector siempre activo es `DemoFixtureConnector`, **sintético y sin red**.

Activar un conector es una decisión consciente del operador, bajo su responsabilidad
legal, y **sólo** para fuentes cuya evaluación (términos + `robots.txt`) lo permita.
