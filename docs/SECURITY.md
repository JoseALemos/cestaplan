# Modelo de seguridad — CestaPlan

Este documento describe el modelo de seguridad de CestaPlan: cómo se autentican los
usuarios, cómo se autorizan las acciones dentro de un hogar, qué protecciones aplica
el servidor y cómo reportar una vulnerabilidad. Es coherente con las decisiones
canónicas del proyecto y con `docs/PRIVACY.md`.

Principio rector: **OpenAI propone; el núcleo determinista valida y calcula.** Ningún
componente de IA participa en decisiones de seguridad (autenticación, autorización,
validación de alergias o cálculo económico). Toda función crítica funciona sin OpenAI.

---

## 1. Autenticación

### 1.1 Credenciales

- Autenticación por **email + contraseña**. Sin OAuth de terceros en el MVP.
- Las contraseñas se almacenan con **Argon2id** (nunca en claro, nunca con hashes
  rápidos tipo MD5/SHA-1/SHA-256 sin KDF). Parámetros de coste (memoria, iteraciones,
  paralelismo) configurables y revisados periódicamente.
- Política mínima de contraseña: longitud mínima razonable y verificación contra una
  lista de contraseñas comprometidas/comunes. No se imponen reglas de composición
  arbitrarias que empeoran la usabilidad sin mejorar la seguridad.

### 1.2 Sesiones opacas en base de datos

- El servidor emite **sesiones OPACAS**: el identificador de sesión es un valor
  aleatorio sin significado. El estado autoritativo vive en la tabla `UserSession` de
  PostgreSQL.
- **NO se usan JWT de larga duración**, y en ningún caso se almacenan tokens de sesión
  en `localStorage` ni en `sessionStorage` del navegador.
- Cada registro de sesión guarda, como mínimo: usuario, hash del token de sesión,
  fecha de creación, `expires_at`, última actividad, y metadatos de auditoría
  (user-agent, IP truncada). El token en claro nunca se persiste; se guarda su hash.

### 1.3 Cookie de sesión

La sesión viaja en una cookie con estos atributos:

| Atributo    | Valor                          | Motivo                                            |
|-------------|--------------------------------|---------------------------------------------------|
| `HttpOnly`  | siempre                        | Inaccesible desde JavaScript (mitiga robo por XSS)|
| `Secure`    | `true` en producción           | Solo se envía sobre HTTPS (`COOKIE_SECURE`)       |
| `SameSite`  | `lax`/`strict` (`COOKIE_SAMESITE`) | Reduce superficie CSRF                        |
| `Path`      | `/`                            | Alcance de la aplicación                          |
| Sin `Domain`| host-only                      | No se comparte con subdominios innecesarios       |

`COOKIE_SECURE` y `COOKIE_SAMESITE` se controlan por variable de entorno (ver
`.env.example`). En producción, `COOKIE_SECURE=true` es obligatorio.

### 1.4 Expiración, revocación y rotación

- Cada sesión tiene expiración absoluta (`SESSION_TTL_HOURS`, por defecto 720 h) y se
  valida en cada petición contra la base de datos.
- **Logout** revoca la sesión en servidor (borrado o marcado como revocada), no basta
  con borrar la cookie del cliente.
- El usuario puede revocar sesiones individualmente; un cambio de contraseña revoca
  todas las sesiones activas del usuario.
- El material de firma/rotación de sesión se deriva de `SESSION_SECRET` (32+ bytes
  aleatorios). Rotar `SESSION_SECRET` invalida sesiones existentes.

### 1.5 Recuperación de contraseña (preparada)

- Flujo de recuperación **preparado** en el modelo: tokens de un solo uso, de vida
  corta, ligados al usuario, hasheados en BD, invalidados tras el uso o al expirar.
- La respuesta del endpoint de solicitud es **uniforme** exista o no la cuenta (no
  revela si un email está registrado).
- El envío efectivo de correo depende de la configuración del despliegue; el
  mecanismo de tokens no expone secretos en logs.

### 1.6 Rate limiting en login

- Los endpoints de autenticación (login, solicitud de recuperación) aplican
  **rate limiting** por IP y por cuenta, con backoff, para frenar credential stuffing
  y fuerza bruta.
- Los intentos fallidos se registran en `AuditLog` sin almacenar la contraseña
  probada.

---

## 2. Protección CSRF

- Todas las **mutaciones** (POST/PUT/PATCH/DELETE) exigen protección CSRF.
- Estrategia: `SameSite` en la cookie de sesión + **token anti-CSRF** verificado en el
  servidor para peticiones que cambian estado (patrón double-submit o token por
  sesión). Las peticiones idempotentes de solo lectura (GET) no lo requieren.
- El front (Next.js) adjunta el token en una cabecera; el API lo valida antes de
  ejecutar la mutación.

---

## 3. Autorización: permisos por hogar

La unidad de autorización es el **hogar** (`Household`). Un usuario accede a los datos
de un hogar únicamente a través de su pertenencia (`HouseholdMember`), que lleva un rol.

### 3.1 Roles

| Rol      | Puede leer | Puede editar planes/datos | Puede gestionar miembros e invitaciones | Puede borrar el hogar |
|----------|:----------:|:-------------------------:|:---------------------------------------:|:---------------------:|
| `owner`  | Sí         | Sí                        | Sí                                      | Sí                    |
| `editor` | Sí         | Sí                        | No                                      | No                    |
| `viewer` | Sí         | No                        | No                                      | No                    |

- Cada hogar tiene al menos un `owner`. No se puede quedar sin propietario.
- Las invitaciones (`HouseholdInvitation`) usan tokens de un solo uso, con expiración.

### 3.2 Autorización a nivel de recurso

- **Toda** consulta y mutación filtra por el hogar al que pertenece el usuario
  autenticado. No se confía en identificadores enviados por el cliente para saltarse
  el filtro (no IDOR): pertenencia y rol se verifican en servidor en cada operación.
- Los identificadores públicos son **UUID** (no secuenciales), pero eso es defensa en
  profundidad, no la autorización en sí.
- Los intentos de acceso denegado se registran en `AuditLog`.

---

## 4. CORS restrictivo

- El API solo acepta orígenes de una **lista blanca explícita**
  (`CORS_ALLOWED_ORIGINS`, separada por comas). Sin comodín `*`.
- `Access-Control-Allow-Credentials: true` se combina únicamente con orígenes
  concretos (nunca con `*`, que sería incompatible y peligroso con cookies).
- Métodos y cabeceras permitidos se limitan a los que la aplicación usa.

---

## 5. Cabeceras de seguridad

El API y el web devuelven un conjunto explícito de cabeceras de seguridad. Lista
concreta:

| Cabecera                          | Valor (base)                                                        | Propósito                                             |
|-----------------------------------|---------------------------------------------------------------------|-------------------------------------------------------|
| `Content-Security-Policy`         | `default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'` | Mitiga XSS e inyección de recursos |
| `Strict-Transport-Security`       | `max-age=63072000; includeSubDomains; preload`                      | Fuerza HTTPS (HSTS). Solo en producción sobre TLS     |
| `X-Content-Type-Options`          | `nosniff`                                                           | Evita MIME sniffing                                    |
| `Referrer-Policy`                 | `strict-origin-when-cross-origin`                                   | Limita fuga de URLs en el `Referer`                   |
| `X-Frame-Options`                 | `DENY`                                                             | Anti-clickjacking (redundante con `frame-ancestors`)  |
| `Cross-Origin-Opener-Policy`      | `same-origin`                                                      | Aísla el contexto de navegación                       |
| `Cross-Origin-Resource-Policy`    | `same-origin`                                                      | Restringe carga cruzada de recursos                   |
| `Permissions-Policy`              | `geolocation=(), camera=(), microphone=()`                         | Desactiva APIs del navegador no usadas                |
| `X-Permitted-Cross-Domain-Policies` | `none`                                                          | Bloquea políticas cross-domain heredadas              |

Notas:
- La CSP se endurece a medida que se conocen los orígenes reales de assets del front
  (evitar `unsafe-inline` en producción; usar nonces/hashes para estilos/scripts si
  hicieran falta).
- `HSTS` solo se emite cuando el despliegue sirve HTTPS (en Railway, siempre en los
  servicios públicos).

---

## 6. Validación estricta y límites de tamaño

- Toda entrada del API se valida con **Pydantic v2** (backend) y **Zod** (contratos
  compartidos derivados de los modelos Pydantic). Nada de texto libre fuera de esquema,
  incluidas las respuestas de OpenAI (se validan contra JSON Schema antes de usarse).
- **Fail closed**: entrada inválida se rechaza con 4xx; nunca se coacciona
  silenciosamente un valor peligroso.
- Límites explícitos de tamaño: cuerpo de la petición, longitud de cadenas, número de
  elementos en listas (p. ej. líneas de una lista de compra, número de comidas
  solicitadas), profundidad de objetos y tamaño de importaciones CSV/JSON.
- El dinero viaja como **string decimal** en JSON y se maneja como `Decimal`/`numeric`;
  nunca `float`. La validación rechaza formatos monetarios ambiguos.
- Las subidas de importación (adaptadores CSV/JSON) tienen límite de tamaño y se
  procesan sin ejecutar contenido.

---

## 7. Gestión de secretos

- Todos los secretos provienen de **variables de entorno**; nunca se codifican en el
  repositorio. `.env` está en `.gitignore`; solo se versiona `.env.example`.
- Secretos gestionados: `SESSION_SECRET`, `DATABASE_URL`, `OPENAI_API_KEY` (cuando
  `AI_BILLING_MODE != disabled`).
- En Railway, los secretos se inyectan por servicio/entorno; `DATABASE_URL` se comparte
  por **red privada** (ver `docs/DEPLOYMENT.md`).
- Los secretos **nunca** se registran en logs, ni se envían a OpenAI, ni se exponen en
  respuestas de error. En modo `cloud`, la `OPENAI_API_KEY` gestionada por la plataforma
  no se revela al cliente ni al usuario final.
- Rotación: `SESSION_SECRET` y claves de terceros pueden rotarse por entorno. Rotar
  invalida el material dependiente (p. ej. sesiones).

---

## 8. Escaneo de dependencias en CI

- **GitHub Actions** ejecuta, en cada PR y de forma programada:
  - Auditoría de dependencias Python (p. ej. `pip-audit` / `uv` sobre el lockfile) y
    JS (`pnpm audit`).
  - Análisis estático de código (`ruff`, `pyright`, `eslint`) y detección de secretos
    filtrados en el diff.
  - Actualizaciones de dependencias supervisadas (p. ej. Dependabot).
- El pipeline debe fallar ante vulnerabilidades de severidad alta/crítica sin
  mitigación documentada.
- No hay autodeploy desde forks no confiables (ver `docs/DEPLOYMENT.md`): los secretos
  de CI no se exponen a PRs de forks.

---

## 9. Modelo de amenazas (breve)

Alcance: aplicación web/PWA multiusuario con datos por hogar, un backend FastAPI, un
worker de cola en PostgreSQL y una integración opcional con OpenAI.

| # | Amenaza                                              | Vector                                   | Mitigación principal                                                        |
|---|------------------------------------------------------|------------------------------------------|-----------------------------------------------------------------------------|
| 1 | Robo de credenciales / fuerza bruta                  | Login público                            | Argon2id, rate limiting, respuestas uniformes, auditoría                    |
| 2 | Secuestro de sesión                                  | Robo de cookie/token                     | Cookies `HttpOnly`/`Secure`/`SameSite`, sesiones opacas revocables en BD    |
| 3 | XSS                                                  | Entrada renderizada en el front          | CSP estricta, `HttpOnly`, escape por defecto de React, validación de salida |
| 4 | CSRF                                                 | Petición cruzada con cookie              | Token anti-CSRF en mutaciones + `SameSite`                                  |
| 5 | Acceso no autorizado entre hogares (IDOR)            | Manipular IDs en peticiones              | Autorización por pertenencia+rol en servidor, UUID públicos                 |
| 6 | Escalada de privilegios dentro del hogar             | `viewer`/`editor` intentando gestionar   | Comprobación de rol por operación                                          |
| 7 | Inyección SQL                                        | Parámetros de consulta                    | SQLAlchemy 2 parametrizado, sin SQL por concatenación                       |
| 8 | Fuga de datos sensibles a OpenAI                     | Contexto enviado al LLM                   | Pseudonimización: nunca nombres reales, email ni IDs internos (ver PRIVACY) |
| 9 | Fuga de secretos                                     | Logs, errores, repositorio               | Secretos en entorno, `.gitignore`, sin secretos en logs/errores            |
| 10| Dependencias vulnerables                             | Cadena de suministro                     | Escaneo en CI, lockfiles, actualizaciones supervisadas                     |
| 11| Abuso/DoS de generación con IA                       | Peticiones masivas de planes             | Cola en Postgres con reintentos limitados, cuotas (`UsageLedger`) en cloud  |
| 12| Datos económicos manipulados/caducados               | Importaciones o precios erróneos         | Validación estricta, `verification_status`, no usar datos caducados         |

Fuera de alcance del MVP: pagos, integraciones OAuth de terceros, scraping, elusión de
CAPTCHA/anti-bot (prohibidos por diseño).

---

## 10. Cómo reportar una vulnerabilidad

Agradecemos la divulgación responsable. Si crees haber encontrado una vulnerabilidad de
seguridad en CestaPlan:

1. **No abras un issue público** ni la divulgues antes de que exista una corrección.
2. Envía un informe privado usando el canal de **avisos de seguridad de GitHub**
   (*Security advisories → Report a vulnerability*) de este repositorio, o por correo
   al responsable de mantenimiento indicado en el `README`.
3. Incluye, si puedes: descripción, pasos de reproducción, impacto estimado, versión o
   commit afectado, y cualquier prueba de concepto.

Compromiso del proyecto:

- **Acuse de recibo** en un plazo razonable (objetivo: 72 horas).
- Evaluación y, si procede, plan de corrección con severidad estimada.
- **Divulgación coordinada**: publicaremos el aviso y el crédito (si lo deseas) una vez
  disponible la corrección.
- No emprenderemos acciones contra quien investigue de buena fe, sin acceder a datos de
  terceros más allá de lo necesario, sin degradar el servicio y sin exfiltrar datos.

Este proceso se resume también en el `SECURITY.md` de la raíz del repositorio.
