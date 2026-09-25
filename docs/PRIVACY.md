# Privacidad — CestaPlan

Este documento describe cómo CestaPlan trata los datos personales y qué garantías ofrece
al usuario. Es coherente con las decisiones canónicas del proyecto y con
`docs/SECURITY.md`. CestaPlan es software de código abierto y auto-alojable: el
responsable del tratamiento efectivo es quien opera cada despliegue.

> **Aviso (disclaimer sanitario obligatorio)**:
>
> CestaPlan facilita la planificación y ofrece información orientativa. No sustituye el consejo de un profesional sanitario. Comprueba siempre las etiquetas de los productos en caso de alergia o intolerancia.

---

## 1. Datos sensibles de aplicación

Las **alergias**, los **objetivos nutricionales** y las **preferencias alimentarias**
son datos que revelan información sobre la salud y los hábitos del usuario. En CestaPlan
se tratan como **datos sensibles de aplicación**, con protección reforzada:

- Se usan exclusivamente para la función que el usuario ha solicitado (planificar
  comidas seguras y adecuadas).
- Nunca se envían en claro ni asociados a la identidad real del usuario a terceros
  (ver sección 5, pseudonimización hacia OpenAI).
- Las alergias son una **restricción dura**: las valida el núcleo determinista
  (`AllergenValidator`), nunca la IA.

---

## 2. Principios

1. **Minimización de datos.** Solo se recogen los datos necesarios para la
   funcionalidad. No se pide información que no se use.
2. **Propósito limitado.** Los datos se usan para planificar comidas, no para perfilado
   publicitario ni para fines ajenos.
3. **Exportación de cuenta.** El usuario puede exportar sus datos (perfil, hogar,
   planes, listas, favoritos) en un formato legible por máquina.
4. **Eliminación de cuenta.** El usuario puede eliminar su cuenta. La eliminación
   realiza **borrado real** de los datos personales o su **anonimización** cuando deba
   conservarse un registro por integridad referencial (ver sección 3).
5. **Consentimiento específico para OpenAI.** El uso de IA es **opt-in**. Sin
   consentimiento explícito, no se envía ningún contexto a OpenAI.
6. **Poder desactivar la IA.** Toda función crítica funciona sin OpenAI. El usuario (o
   el administrador del despliegue, vía `AI_BILLING_MODE=disabled`) puede desactivar la
   IA por completo y seguir usando el producto.

---

## 3. Eliminación y anonimización de cuenta

- El usuario puede **suprimir su cuenta** (art. 17 RGPD) desde el endpoint autenticado
  `POST /api/v1/auth/account/delete` (requiere confirmar repitiendo su email + CSRF). La
  operación es **irreversible** y hace: revoca todas las sesiones; **anonimiza** la cuenta
  (email/nombre/consentimiento sustituidos por marcadores, contraseña inutilizada,
  `status='anonymized'`); elimina las membresías de hogar del usuario; hace **soft-delete**
  de los hogares que posee y de los que es el único miembro; y **redacta el email** en sus
  entradas de `AuditLog`.
- La fila `user` se **conserva anonimizada** (no borrada) porque la auditoría y los datos de
  hogar la referencian por id; al disociarla de la persona deja de ser dato personal.
- **Caso a revisar (pendiente de decisión del responsable):** un hogar que el usuario posee
  pero que tiene **otros miembros** se conserva (dato de terceros); no se fuerza la
  transferencia de propiedad. Documentado para valorarlo con el asesor.
- El **soft delete** se usa solo donde es imprescindible; no es un sustituto del borrado
  real de datos personales.
- Los datos demo (`is_synthetic=true`) no son datos personales y no se ven afectados.
- **Retención de auditoría:** el `audit_log` se purga por retención (`AUDIT_RETENTION_DAYS`,
  por defecto 365 días; job `purge_audit_logs`). El responsable confirma el periodo.

---

## 4. Consentimiento y control de la IA

- El envío de datos a OpenAI requiere **consentimiento específico e informado**,
  separado del alta en el servicio.
- El consentimiento es **revocable** en cualquier momento; al revocarlo, cesan los
  envíos a OpenAI.
- Modos de facturación de IA (`AI_BILLING_MODE`):
  - `disabled`: la IA está apagada; nunca se contacta con OpenAI.
  - `byok`: el usuario/administrador aporta su propia `OPENAI_API_KEY`.
  - `platform`: la plataforma gestiona la clave, registra el consumo (`UsageLedger`) y
    aplica cuotas; **nunca revela la clave** al cliente.
- El administrador de un despliegue self-hosted puede desactivar la IA globalmente.

---

## 5. Pseudonimización del contexto enviado a OpenAI

Cuando la IA está activada y consentida, el contexto que se envía a OpenAI se
**pseudonimiza** antes de salir del backend:

- **NUNCA** se envían nombres reales, direcciones de email ni **identificadores
  internos** (PK, UUID de usuario/hogar, IDs de sesión).
- El contexto se reduce a lo estrictamente necesario para proponer recetas: tipos de
  comida, restricciones expresadas de forma abstracta, etiquetas de preferencia,
  equipamiento disponible y parámetros de la petición.
- Las alergias se comunican como categorías de restricción, no como perfil identificado.
- La respuesta de OpenAI se valida contra JSON Schema y pasa por el flujo determinista
  de 12 pasos antes de usarse; OpenAI no decide seguridad de alergias, precios ni
  cálculos.
- OpenAI se usa solo para lo permitido (proponer recetas, redactar instrucciones,
  clasificar estilos, sugerir sustituciones, normalizar texto libre sujeto a
  validación).

---

## 6. Política de logs

- Los logs registran lo necesario para operar y auditar (errores, eventos de seguridad,
  trazas de trabajos), **no** contenido personal innecesario.
- **No se registran recetas privadas completas** ni el contexto personal si no hace
  falta para diagnosticar. Cuando se necesite trazar un fallo, se prefieren
  identificadores opacos y datos mínimos.
- **Nunca** se registran secretos (`SESSION_SECRET`, `DATABASE_URL`, `OPENAI_API_KEY`),
  contraseñas ni tokens de sesión en claro.
- Las IP se almacenan truncadas o con retención acotada cuando se usan para seguridad.

---

## 7. Auditoría administrativa

- Las acciones sensibles (accesos denegados, cambios de rol, invitaciones, eliminación
  de cuenta, cambios de configuración de IA) se registran en `AuditLog`.
- El registro de auditoría es para **rendición de cuentas y seguridad**, con acceso
  restringido a administradores del despliegue.
- Las entradas de auditoría se conservan de forma que puedan anonimizarse si el sujeto
  elimina su cuenta, preservando la integridad del registro sin retener datos
  personales innecesarios.

---

## 8. Tabla de datos personales

Base jurídica orientativa (RGPD); el responsable de cada despliegue debe confirmarla
según su jurisdicción y su relación con los usuarios.

| Dato                                   | Finalidad                                                | Base                                | Retención                                             |
|----------------------------------------|----------------------------------------------------------|-------------------------------------|-------------------------------------------------------|
| Email                                  | Identificación, login, recuperación                      | Ejecución del contrato              | Mientras exista la cuenta; se borra al eliminarla     |
| Hash de contraseña (Argon2id)          | Autenticación                                            | Ejecución del contrato              | Mientras exista la cuenta                             |
| Sesiones (`UserSession`)               | Mantener la sesión iniciada                              | Ejecución del contrato              | Hasta expiración/revocación; se purgan las caducadas  |
| Pertenencia y rol en el hogar          | Autorización por hogar                                   | Ejecución del contrato              | Mientras exista la pertenencia                        |
| Perfil dietético / objetivos           | Adecuar la planificación (dato sensible)                 | Consentimiento                      | Mientras exista la cuenta; borrado al eliminarla      |
| Alergias / restricciones               | Seguridad alimentaria — restricción dura (dato sensible) | Consentimiento                      | Mientras exista la cuenta; borrado al eliminarla      |
| Preferencias y favoritos               | Personalizar propuestas (dato sensible)                  | Consentimiento                      | Mientras exista la cuenta; borrado al eliminarla      |
| Equipamiento del hogar                 | Filtrar recetas por equipo disponible                    | Ejecución del contrato              | Mientras exista la cuenta                             |
| Dirección/CP del hogar (opcional)      | Coste de desplazamiento y geocerco de sede (ver §10)     | Consentimiento / contrato           | Mientras se declare; ⚠️ se envía a Nominatim/OSM (§10) |
| Planes, listas y feedback              | Función principal del producto                           | Ejecución del contrato              | Mientras exista la cuenta; borrado al eliminarla      |
| Contexto pseudonimizado a OpenAI       | Proponer recetas candidatas                              | Consentimiento específico (opt-in)  | No se conserva más allá de lo necesario; sin PII      |
| Consumo de IA (`UsageLedger`, cloud)   | Cuotas y control de coste                                | Interés legítimo / contrato         | Agregado; se anonimiza al eliminar la cuenta          |
| Registros de auditoría (`AuditLog`)    | Seguridad y rendición de cuentas                         | Interés legítimo / obligación legal | Retención acotada; se anonimiza al eliminar la cuenta |
| Logs técnicos (errores, IP truncada)   | Operación y seguridad                                    | Interés legítimo                    | Retención corta y acotada                             |

---

## 9. Derechos del usuario

El usuario puede, sobre sus datos: acceder, exportar, rectificar (editando su perfil),
retirar el consentimiento de IA y **eliminar su cuenta** (borrado real o anonimización).
En despliegues sujetos al RGPD u otras normativas, el operador debe atender además los
derechos aplicables (oposición, limitación, portabilidad) según su rol de responsable
del tratamiento.

---

## 10. Geocodificación de direcciones (tercero: Nominatim / OpenStreetMap)

> ⚠️ Divulgación factual del flujo de datos, **pendiente de validación por el asesor** antes de
> publicarse como declaración legal.

Cuando el hogar declara una **dirección/código postal** (opcional, para el coste de desplazamiento
y el geocerco de sede), CestaPlan la **envía a un tercero** para obtener coordenadas:

- **Nominatim (OpenStreetMap Foundation)** — `nominatim.openstreetmap.org`: recibe el texto de la
  dirección, el código postal y la ciudad para geocodificarlos.
- **Overpass (OpenStreetMap)** — recibe **coordenadas** (no la dirección) para localizar tiendas
  cercanas.

Notas:
- La dirección postal es un **dato personal**; su envío a la OSM Foundation es una comunicación a un
  tercero que debe informarse aquí y, según el caso, valorarse como **transferencia**.
- El destino es un **host fijo por configuración** (no lo controla el usuario): no es un SSRF.
- La función es **desactivable** por configuración (`GEO_ENABLED`) y solo actúa si el hogar declara
  dirección. Se cumple la *usage policy* de Nominatim (cabeceras User-Agent/From).
- **Alternativa** para máxima minimización: auto-hospedar el geocoder (Nominatim propio) para no
  enviar direcciones a un tercero.

Acciones pendientes del responsable: confirmar base jurídica, informar este flujo en el aviso de
privacidad público y decidir si se ofrece como opt-in explícito o se auto-hospeda el geocoder.
