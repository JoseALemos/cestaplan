# 0005 — Sesiones opacas en base de datos, no JWT en el cliente

- **Estado:** Aceptado
- **Fecha:** 2026-07-21
- **Decisores:** Equipo fundador CestaPlan

## Contexto y problema

Necesitamos autenticación con registro por email/contraseña, cierre de sesión inmediato,
revocación, expiración y permisos por hogar. Manejamos datos sensibles de aplicación (alergias,
objetivos, preferencias), por lo que la gestión de sesiones debe permitir revocación real y no
exponer secretos de larga duración en el navegador.

## Opciones consideradas

1. **JWT de larga duración en localStorage.** Cómodo pero inseguro: no se puede revocar antes de
   expirar y es vulnerable a XSS al ser accesible por JavaScript. Descartado por el encargo.
2. **JWT de acceso corto + refresh token.** Revocación parcial vía lista de refresh; más complejidad
   y aún así el token de acceso vive en el cliente.
3. **Sesiones opacas almacenadas en la base de datos**, referenciadas por una cookie HttpOnly.

## Decisión

Adoptamos la opción 3. Al iniciar sesión se crea un registro `UserSession` con un identificador
opaco de alta entropía (se almacena su hash, no el valor en claro). El cliente recibe una cookie
**HttpOnly**, **Secure** en producción y con **SameSite** apropiado. La sesión tiene expiración y
puede revocarse (logout, cambio de contraseña, acción de admin). Las contraseñas se hashean con
**Argon2id**. Las operaciones mutables exigen protección **CSRF**; el login tiene **rate limiting**.
Los permisos se verifican por hogar con roles `owner`/`editor`/`viewer`. **No se almacenan JWT de
larga duración en localStorage.**

## Consecuencias

- **Positivas:** revocación inmediata, no hay secretos accesibles por JS, expiración controlada por
  el servidor, auditable.
- **Negativas / coste asumido:** cada petición autenticada consulta la sesión en BD (mitigable con
  índice por hash y caché ligera); requiere gestión de CSRF explícita.
- **Seguimiento:** si la carga de lectura de sesiones crece, evaluar caché en memoria con
  invalidación por revocación. No introducir Redis solo para esto sin datos que lo justifiquen.
