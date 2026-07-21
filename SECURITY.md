# Política de seguridad

Nos tomamos en serio la seguridad de CestaPlan y agradecemos la divulgación
responsable de vulnerabilidades. Este documento es un resumen; el detalle técnico
vive en [docs/SECURITY.md](./docs/SECURITY.md).

## Cómo reportar una vulnerabilidad

**No abras un issue público** para vulnerabilidades de seguridad. Repórtalas por un
canal privado:

- Preferentemente, mediante **GitHub Security Advisories** ("Report a vulnerability"
  en la pestaña *Security* del repositorio).
- O por correo a: **`security@cestaplan.example`** *(placeholder — sustituir por un
  contacto real antes de publicar el proyecto)*.

Incluye, si puedes:

- Descripción de la vulnerabilidad y su impacto.
- Pasos para reproducirla (prueba de concepto).
- Versión / commit afectado y entorno.
- Cualquier mitigación conocida.

## Qué esperar

- **Acuse de recibo** en un plazo razonable (objetivo: 72 horas laborables).
- Trabajaremos contigo para **validar y corregir** el problema y coordinar la
  divulgación.
- Te daremos **crédito** por el hallazgo si así lo deseas.

Te pedimos que nos des un plazo razonable para publicar una corrección antes de
divulgar públicamente los detalles.

## Divulgación responsable

Al investigar, por favor:

- No accedas ni modifiques datos que no sean tuyos.
- No degrades el servicio (sin DoS ni fuerza bruta agresiva).
- Respeta la privacidad; no exfiltres datos personales.
- No realices *scraping* ni eludas medidas anti-bot (además es contrario a los
  principios del proyecto).

## Versiones soportadas

El proyecto está en desarrollo activo (pre-1.0). Solo se da soporte de seguridad a
la **última versión de la rama principal**. Las etiquetas de versión anteriores no
reciben parches retroactivos salvo indicación expresa.

| Versión | Soporte de seguridad |
|---------|----------------------|
| `main` (última) | Sí |
| Versiones anteriores | No |

Para el detalle sobre modelo de amenazas, autenticación (sesiones opacas, Argon2id,
CSRF, rate limiting), CORS, cabeceras y manejo de secretos, consulta
[docs/SECURITY.md](./docs/SECURITY.md) y [docs/PRIVACY.md](./docs/PRIVACY.md).
