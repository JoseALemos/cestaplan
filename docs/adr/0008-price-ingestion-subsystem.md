# 0008 — Subsistema de ingesta de precios: arquitectura de conectores, cola Postgres, sin scraping de fuentes bloqueadas

- **Estado:** Aceptado
- **Fecha:** 2026-07-22
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

CestaPlan promete precios con fuente, tienda y fecha, y no inventarlos nunca (ver ADR
[0006](0006-hybrid-data-sources-no-scraping.md)). El §27 de la especificación pide un
**framework responsable de ingesta de precios** que descubra, capture, parsee,
normalice, valide y versione precios de supermercado desde fuentes legales, con
historial y cobertura honesta, sin depender de técnicas prohibidas ni de
infraestructura pesada. Hay que decidir: el modelo de conectores, el sustrato de la
cola, las claves primarias, y qué hacer con las cadenas cuyos endpoints de datos están
prohibidos por `robots.txt`.

## Opciones consideradas

1. **Scraper monolítico por cadena** con lógica de red embebida en cada scraper.
   Frágil, difícil de testear, y mezcla la política de acceso con el parseo.
2. **Cola con Redis/Celery** para orquestar los crawls. Añade un servicio más que
   operar en Railway y en autohospedaje, contra el principio de mínima superficie.
3. **PK con `uuid` como clave primaria** en todas las tablas nuevas. Rompe el patrón
   ya establecido en el resto del modelo y penaliza los índices/joins.
4. **Framework de conectores desacoplados + cola sobre Postgres + PK bigint con
   `public_id` uuid**, con las fuentes bloqueadas marcadas `permission_required` y
   nunca rastreadas, y Open Prices como primer conector real (ODbL).

## Decisión

Adoptamos la opción 4.

- **Conectores desacoplados.** Contrato `RetailerConnector` (`ingestion/contracts.py`)
  sin dependencias del ORM, con `Capabilities` y `SourcePolicy` declarativos y
  degradación elegante (métodos por defecto devuelven "no soportado", nunca lanzan).
  Toda la red pasa por un único `HttpFetcher` (timeouts, backoff+jitter, límites por
  dominio, conditional-GET, circuit breaker, guardia SSRF + allowlist, redacción de
  secretos y detección —que sólo reporta— de páginas de bloqueo/CAPTCHA). El worker no
  conoce ningún conector concreto: los resuelve por `registry`.

- **Cola sobre PostgreSQL, sin Redis/Celery.** La cola `CrawlJob`
  (`ingestion/queue.py`) usa `SELECT ... FOR UPDATE SKIP LOCKED`, con heartbeat,
  backoff, dead-letter, recuperación de stuck-jobs, límites por retailer e idempotencia
  por clave. El `CrawlScheduler` es idempotente (advisory lock + freshness por
  cadencia). Coherente con la cola de planes (ADR
  [0002](0002-postgres-job-queue-no-redis.md)): una dependencia menos, autohospedaje
  trivial, transaccionalidad con el resto del modelo, y visibilidad vía SQL.

- **Reutilización del patrón de PK `bigint` + `public_id` uuid** en vez de `uuid` como
  PK. Cada tabla nueva de `models/ingestion.py` lleva su identidad interna `bigint`
  (índices/joins compactos) más un `uuid` público estable para las URLs de la API. Es
  el patrón ya usado en el resto del modelo; no se introduce una convención nueva.

- **`permission_required` para fuentes prohibidas por `robots.txt`.** Mercadona
  (`Disallow: /api`), Carrefour (`Disallow: /supermercado/ajax/*`) y Lidl
  (`Disallow: /user-api/*`) prohíben sus endpoints de datos: sus conectores existen
  como framework pero quedan **desactivados y nunca se rastrean**. `robots.txt` no se
  trata como autorización. Dia/Alcampo/Deza quedan `partial_only`/`unsupported` con
  evaluación por fuente pendiente. Ver
  [`../RETAILER_SOURCE_MATRIX.md`](../RETAILER_SOURCE_MATRIX.md) y
  [`../SCRAPING_POLICY.md`](../SCRAPING_POLICY.md).

- **Open Prices como primer conector real (ODbL).** Fuente pública, legal y con
  atribución. Es la única fuente de precios reales activa; su cobertura se reporta con
  honestidad y nunca como completa.

- **Todo opt-in, desactivado por defecto.** `SCRAPING_ENABLED=false` y un flag
  `*_CONNECTOR_ENABLED=false` por conector. El único siempre activo es
  `DemoFixtureConnector`, sintético y sin red.

## Consecuencias

- **Positivas:** legalidad y honestidad de datos; una dependencia de infraestructura
  menos (sin Redis/Celery); conectores testeables en aislamiento; aislamiento de
  fallos (un conector caído no afecta a los demás); PK coherente con el modelo
  existente; historial append-only y cobertura auditable.
- **Negativas / coste asumido:** menor throughput que un broker dedicado; la utilidad
  de precios reales depende hoy de Open Prices (cobertura desigual) mientras las
  cadenas mayores sigan `permission_required`. Aceptable: el proyecto prioriza
  legalidad sobre cobertura.
- **Seguimiento:** si el volumen de jobs excede lo que Postgres soporta cómodamente,
  reconsiderar `LISTEN/NOTIFY` o un broker (la interfaz del worker lo permite sin tocar
  la lógica de negocio). Cualquier conector nuevo pasa por revisión de licencia y
  términos, documentada en la matriz de fuentes y en `SourceAuditService`. Si una
  cadena ofreciera un feed/API con permiso explícito, su conector pasaría de
  `permission_required` a `authorized`.
