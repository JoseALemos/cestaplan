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

## Corrección correcta (preserva la seguridad)

Servir web y API bajo el **mismo dominio registrable**, con dominios personalizados en Railway:

- p. ej. `app.tudominio.com` (web) + `api.tudominio.com` (API), o un único dominio con la API bajo
  `/api`. Así las cookies son **first-party** y se envían con normalidad → se arregla a la vez la
  **sesión** y el **CSRF**.
- Ajustar `CORS_ORIGINS` al nuevo origen de la web y `NEXT_PUBLIC_API_BASE_URL` al de la API.

**No** se debe: ampliar CORS a `*`, desactivar CSRF, relajar SameSite/permisos ni pasar la sesión
a `localStorage` (introduce riesgo XSS). Ninguna de esas opciones es una corrección válida.

## Qué incluye esta PR (no resuelve el 403 por sí sola)

El 403 se resuelve con el cambio de dominio anterior. Esta rama mejora la robustez y el diagnóstico:

- **Mensaje útil**: al fallar la finalización con 403/401 se muestra una causa comprensible
  (posible bloqueo de cookies entre dominios) y la acción de reintentar/iniciar sesión, sin exponer
  tokens ni internos (`lib/onboarding/finalize-error.ts`).
- **`/despensa` sin recargar**: tras crear el hogar se invalida la query `households`, de modo que
  la despensa y la cabecera reconocen el hogar de inmediato (arregla el hueco de caché de §9 para
  cuando el 403 esté resuelto).
- **Tests de regresión** que fijan el contrato CSRF de la finalización (ausente/ inválido/ éxito →
  propietario + miembro) y el mapeo de mensajes.

## Estado de datos (sin cambios)

No se sembraron productos ni precios; no se crearon hogares/usuarios; la producción de proveedores
sigue desactivada. `/price-providers` ya lista las fuentes **autorizadas**; `/retailers` seguirá
devolviendo `[]` y `/precios` vacío hasta una sincronización/importación real (tarea aparte).
