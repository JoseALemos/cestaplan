# 403 al finalizar el onboarding (cloud) — causa raíz y corrección

## Síntoma

Al finalizar el onboarding, `POST /api/v1/households` devuelve **HTTP 403
`{"detail":"Token CSRF ausente"}`**. No se crea el hogar; `/despensa` muestra
«Primero crea un hogar».

## Causa raíz (demostrada)

Es un problema de **topología de despliegue**, no de código ni de una variable mal puesta.

- La web y la API se sirven en **dominios registrables distintos**:
  `web-production-…up.railway.app` y `api-production-…up.railway.app`.
- `up.railway.app` está en la **Public Suffix List** (lo registró Railway), por lo que cada
  servicio `*.up.railway.app` es un **sitio distinto** → las dos apps son **cross-site**.
- Las cookies `cestaplan_session` y `cestaplan_csrf` las fija la API en su origen. Desde la web
  (otro sitio) son **cookies de terceros**; los navegadores modernos **no las reenvían** en
  peticiones cross-site.
- `verify_csrf` (dependencia que corre **antes** de la autenticación) exige la cookie
  `cestaplan_csrf`. Al no llegar → **403 «Token CSRF ausente»** antes incluso de comprobar la
  sesión.

Lo verificado (sin exponer secretos):

- Config **correcta**: `COOKIE_SECURE=true`, `COOKIE_SAMESITE=none`,
  `CORS_ORIGINS=[web origin]`, `allow_credentials=true`. El preflight OPTIONS responde 200 con el
  origen y `x-csrf-token` permitidos.
- Lógica de servidor **correcta**: con cookie+cabecera CSRF coincidentes → pasa (luego 401 si no
  hay sesión); con cabecera sin cookie → 403 «ausente»; con desajuste → 403 «inválido».
- El frontend **sí** persiste el token CSRF del cuerpo de login y lo envía en la cabecera
  `X-CSRF-Token`; el fallo es la **cookie** cross-site, no la cabecera.
- Auditoría: `user.register` ×1, `auth.login.success` ×1, **cero** escrituras autenticadas.
- La misma naturaleza cross-site afecta también a la cookie de **sesión**.

Clasificación: **`csrf_missing`** (cookie CSRF ausente) causada por el bloqueo de cookies de
terceros en un despliegue cross-site.

## Corrección correcta (preserva la seguridad) — dos opciones válidas

Ambas hacen que las cookies de sesión/CSRF sean **first-party** al host de la web, sin tocar CSRF,
CORS ni permisos. Un dominio personalizado **no** es la única solución.

1. **Dominio registrable común.** Servir web y API bajo el mismo dominio registrable (p. ej.
   `app.tudominio.com` + `api.tudominio.com`). Las cookies pasan a ser first-party y se envían con
   normalidad. Requiere dominios personalizados + DNS.
2. **Proxy same-origin desde el servicio web (la que usa CestaPlan).** El navegador habla **solo**
   con el origen de la web; Next.js reescribe `/api-proxy/...` hacia el API upstream **en el
   servidor**. La API fija sus cookies (`Set-Cookie` host-only) y, al llegar a través del proxy, el
   navegador las asocia al **host de la web** → first-party. No hace falta ningún dominio propio, por
   lo que se conservan los dominios generados por Railway.

CestaPlan usa la **opción 2** porque debe conservar los dominios `*.up.railway.app`.

**Topología final:** `Browser → web Railway (/api-proxy) → proxy → API Railway`.

**No** se debe: ampliar CORS a `*`, desactivar CSRF, relajar SameSite/permisos ni pasar la sesión a
`localStorage` (riesgo XSS). Ninguna de esas opciones es una corrección válida.

## Detalle del proxy same-origin

- `apps/web/next.config.ts` añade una reescritura externa `\/api-proxy/:path* → ${API_UPSTREAM_URL}/:path*`.
- `API_UPSTREAM_URL` (p. ej. `https://api-production-4c5d.up.railway.app`) es **server-only**: nunca
  llega al bundle del navegador. Se valida/normaliza en `src/lib/proxy/upstream.ts` (obligatoria en
  producción, sin barra final, rechaza no-http(s) y evita bucles hacia el propio proxy).
- El navegador usa `NEXT_PUBLIC_API_BASE_URL=/api-proxy` → `apiFetch("/api/v1/…")` pega a
  `/api-proxy/api/v1/…` (mismo origen), con `credentials:"include"` y `X-CSRF-Token`. La cookie
  `cestaplan_csrf` queda ahora legible en el dominio web.
- El proxy de Next conserva método, query, cuerpo (JSON y multipart), `Cookie`/`Set-Cookie`,
  `Content-Type`, códigos HTTP (incl. 204) y streaming, sin registrar cuerpos, cookies ni tokens.
- Las cookies siguen **host-only** (sin `Cookie Domain`), `Secure=true`, `HttpOnly` en sesión,
  `SameSite` sin cambios. La API mantiene su CORS al origen web (aunque el navegador ya no haga CORS
  directo).

## Robustez adicional en esta rama

- **Mensaje útil**: al fallar la finalización con 403/401 se muestra una causa comprensible y la
  acción de reintentar/iniciar sesión, sin exponer tokens ni internos (`lib/onboarding/finalize-error.ts`).
- **`/despensa` sin recargar**: tras crear el hogar se invalida la query `households`.
- **Tests de regresión**: contrato CSRF de la finalización (ausente/inválido/éxito → propietario +
  miembro), normalización del upstream + anti-bucle, y composición de la URL same-origin.

## Estado de datos (sin cambios)

No se sembraron productos ni precios; no se crearon hogares/usuarios; la producción de proveedores
sigue desactivada. `/price-providers` ya lista las fuentes **autorizadas**; `/retailers` seguirá
devolviendo `[]` y `/precios` vacío hasta una sincronización/importación real (tarea aparte).
