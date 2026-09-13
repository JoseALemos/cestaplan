import type { Metadata } from "next";

import { Alert } from "@/components/ui/Alert";

export const metadata: Metadata = {
  title: "Privacidad",
};

export default function PrivacidadPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <header className="mb-8">
        <h1 className="font-display text-display-lg text-ink">Privacidad — CestaPlan</h1>
        <p className="mt-3 text-ink-muted">
          Este documento describe cómo CestaPlan trata los datos personales y qué garantías
          ofrece al usuario. Es coherente con las decisiones canónicas del proyecto y con{" "}
          <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">docs/SECURITY.md</code>.
          CestaPlan es software de código abierto y auto-alojable: el responsable del
          tratamiento efectivo es quien opera cada despliegue.
        </p>
      </header>

      <Alert tone="warning" title="Aviso (disclaimer sanitario obligatorio)">
        CestaPlan facilita la planificación y ofrece información orientativa. No sustituye
        el consejo de un profesional sanitario. Comprueba siempre las etiquetas de los
        productos en caso de alergia o intolerancia.
      </Alert>

      <div className="mt-10 flex flex-col gap-10 text-[0.95rem] text-ink">
        <section>
          <h2 className="font-display text-display-sm text-ink">1. Datos sensibles de aplicación</h2>
          <p className="mt-3 text-ink-muted">
            Las <strong>alergias</strong>, los <strong>objetivos nutricionales</strong> y las{" "}
            <strong>preferencias alimentarias</strong> son datos que revelan información
            sobre la salud y los hábitos del usuario. En CestaPlan se tratan como{" "}
            <strong>datos sensibles de aplicación</strong>, con protección reforzada:
          </p>
          <ul className="mt-3 flex flex-col gap-1.5 pl-5 text-ink-muted [&>li]:list-disc">
            <li>
              Se usan exclusivamente para la función que el usuario ha solicitado
              (planificar comidas seguras y adecuadas).
            </li>
            <li>
              Nunca se envían en claro ni asociados a la identidad real del usuario a
              terceros (ver sección 5, pseudonimización hacia OpenAI).
            </li>
            <li>
              Las alergias son una <strong>restricción dura</strong>: las valida el núcleo
              determinista (
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                AllergenValidator
              </code>
              ), nunca la IA.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">2. Principios</h2>
          <ol className="mt-3 flex flex-col gap-1.5 pl-5 text-ink-muted [&>li]:list-decimal">
            <li>
              <strong>Minimización de datos.</strong> Solo se recogen los datos necesarios
              para la funcionalidad. No se pide información que no se use.
            </li>
            <li>
              <strong>Propósito limitado.</strong> Los datos se usan para planificar
              comidas, no para perfilado publicitario ni para fines ajenos.
            </li>
            <li>
              <strong>Exportación de cuenta.</strong> El usuario puede exportar sus datos
              (perfil, hogar, planes, listas, favoritos) en un formato legible por máquina.
            </li>
            <li>
              <strong>Eliminación de cuenta.</strong> El usuario puede eliminar su cuenta.
              La eliminación realiza <strong>borrado real</strong> de los datos personales o
              su <strong>anonimización</strong> cuando deba conservarse un registro por
              integridad referencial (ver sección 3).
            </li>
            <li>
              <strong>Consentimiento específico para OpenAI.</strong> El uso de IA es{" "}
              <strong>opt-in</strong>. Sin consentimiento explícito, no se envía ningún
              contexto a OpenAI.
            </li>
            <li>
              <strong>Poder desactivar la IA.</strong> Toda función crítica funciona sin
              OpenAI. El usuario (o el administrador del despliegue, vía{" "}
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                AI_BILLING_MODE=disabled
              </code>
              ) puede desactivar la IA por completo y seguir usando el producto.
            </li>
          </ol>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">
            3. Eliminación y anonimización de cuenta
          </h2>
          <ul className="mt-3 flex flex-col gap-1.5 pl-5 text-ink-muted [&>li]:list-disc">
            <li>
              Al eliminar la cuenta, se <strong>borran</strong> los datos personales
              directos: credenciales, email, perfil dietético, alergias, restricciones,
              preferencias, favoritos, sesiones.
            </li>
            <li>
              Cuando un dato forma parte de registros que deben conservarse por integridad
              o auditoría (p. ej. entradas de{" "}
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">AuditLog</code>{" "}
              o agregados de consumo en{" "}
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                UsageLedger
              </code>
              ), se <strong>anonimiza</strong>: se disocia de la persona sustituyendo
              identificadores por valores no reversibles, de modo que dejen de ser datos
              personales.
            </li>
            <li>
              El <strong>soft delete</strong> se usa solo donde es imprescindible; no es un
              sustituto del borrado real de datos personales.
            </li>
            <li>
              Los datos demo (
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                is_synthetic=true
              </code>
              ) no son datos personales y no se ven afectados.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">4. Consentimiento y control de la IA</h2>
          <ul className="mt-3 flex flex-col gap-1.5 pl-5 text-ink-muted [&>li]:list-disc">
            <li>
              El envío de datos a OpenAI requiere <strong>consentimiento específico e
              informado</strong>, separado del alta en el servicio.
            </li>
            <li>
              El consentimiento es <strong>revocable</strong> en cualquier momento; al
              revocarlo, cesan los envíos a OpenAI.
            </li>
            <li>
              Modos de facturación de IA (
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                AI_BILLING_MODE
              </code>
              ):
              <ul className="mt-1.5 flex flex-col gap-1 pl-5 [&>li]:list-[circle]">
                <li>
                  <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                    disabled
                  </code>
                  : la IA está apagada; nunca se contacta con OpenAI.
                </li>
                <li>
                  <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">byok</code>:
                  el usuario/administrador aporta su propia{" "}
                  <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                    OPENAI_API_KEY
                  </code>
                  .
                </li>
                <li>
                  <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                    platform
                  </code>
                  : la plataforma gestiona la clave, registra el consumo (
                  <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                    UsageLedger
                  </code>
                  ) y aplica cuotas; <strong>nunca revela la clave</strong> al cliente.
                </li>
              </ul>
            </li>
            <li>
              El administrador de un despliegue self-hosted puede desactivar la IA
              globalmente.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">
            5. Pseudonimización del contexto enviado a OpenAI
          </h2>
          <p className="mt-3 text-ink-muted">
            Cuando la IA está activada y consentida, el contexto que se envía a OpenAI se{" "}
            <strong>pseudonimiza</strong> antes de salir del backend:
          </p>
          <ul className="mt-3 flex flex-col gap-1.5 pl-5 text-ink-muted [&>li]:list-disc">
            <li>
              <strong>NUNCA</strong> se envían nombres reales, direcciones de email ni{" "}
              <strong>identificadores internos</strong> (PK, UUID de usuario/hogar, IDs de
              sesión).
            </li>
            <li>
              El contexto se reduce a lo estrictamente necesario para proponer recetas:
              tipos de comida, restricciones expresadas de forma abstracta, etiquetas de
              preferencia, equipamiento disponible y parámetros de la petición.
            </li>
            <li>
              Las alergias se comunican como categorías de restricción, no como perfil
              identificado.
            </li>
            <li>
              La respuesta de OpenAI se valida contra JSON Schema y pasa por el flujo
              determinista de 12 pasos antes de usarse; OpenAI no decide seguridad de
              alergias, precios ni cálculos.
            </li>
            <li>
              OpenAI se usa solo para lo permitido (proponer recetas, redactar
              instrucciones, clasificar estilos, sugerir sustituciones, normalizar texto
              libre sujeto a validación).
            </li>
          </ul>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">6. Política de logs</h2>
          <ul className="mt-3 flex flex-col gap-1.5 pl-5 text-ink-muted [&>li]:list-disc">
            <li>
              Los logs registran lo necesario para operar y auditar (errores, eventos de
              seguridad, trazas de trabajos), <strong>no</strong> contenido personal
              innecesario.
            </li>
            <li>
              <strong>No se registran recetas privadas completas</strong> ni el contexto
              personal si no hace falta para diagnosticar. Cuando se necesite trazar un
              fallo, se prefieren identificadores opacos y datos mínimos.
            </li>
            <li>
              <strong>Nunca</strong> se registran secretos (
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                SESSION_SECRET
              </code>
              ,{" "}
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                DATABASE_URL
              </code>
              ,{" "}
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">
                OPENAI_API_KEY
              </code>
              ), contraseñas ni tokens de sesión en claro.
            </li>
            <li>
              Las IP se almacenan truncadas o con retención acotada cuando se usan para
              seguridad.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">7. Auditoría administrativa</h2>
          <ul className="mt-3 flex flex-col gap-1.5 pl-5 text-ink-muted [&>li]:list-disc">
            <li>
              Las acciones sensibles (accesos denegados, cambios de rol, invitaciones,
              eliminación de cuenta, cambios de configuración de IA) se registran en{" "}
              <code className="rounded bg-bg-subtle px-1 py-0.5 text-[0.85em]">AuditLog</code>.
            </li>
            <li>
              El registro de auditoría es para <strong>rendición de cuentas y
              seguridad</strong>, con acceso restringido a administradores del despliegue.
            </li>
            <li>
              Las entradas de auditoría se conservan de forma que puedan anonimizarse si el
              sujeto elimina su cuenta, preservando la integridad del registro sin retener
              datos personales innecesarios.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">8. Tabla de datos personales</h2>
          <p className="mt-3 text-ink-muted">
            Base jurídica orientativa (RGPD); el responsable de cada despliegue debe
            confirmarla según su jurisdicción y su relación con los usuarios.
          </p>
          <div className="mt-3 overflow-x-auto rounded-lg border border-border">
            <table className="w-full min-w-[640px] border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-border bg-bg-subtle">
                  <th className="px-4 py-2.5 font-semibold text-ink">Dato</th>
                  <th className="px-4 py-2.5 font-semibold text-ink">Finalidad</th>
                  <th className="px-4 py-2.5 font-semibold text-ink">Base</th>
                  <th className="px-4 py-2.5 font-semibold text-ink">Retención</th>
                </tr>
              </thead>
              <tbody className="text-ink-muted">
                {[
                  [
                    "Email",
                    "Identificación, login, recuperación",
                    "Ejecución del contrato",
                    "Mientras exista la cuenta; se borra al eliminarla",
                  ],
                  [
                    "Hash de contraseña (Argon2id)",
                    "Autenticación",
                    "Ejecución del contrato",
                    "Mientras exista la cuenta",
                  ],
                  [
                    "Sesiones (UserSession)",
                    "Mantener la sesión iniciada",
                    "Ejecución del contrato",
                    "Hasta expiración/revocación; se purgan las caducadas",
                  ],
                  [
                    "Pertenencia y rol en el hogar",
                    "Autorización por hogar",
                    "Ejecución del contrato",
                    "Mientras exista la pertenencia",
                  ],
                  [
                    "Perfil dietético / objetivos",
                    "Adecuar la planificación (dato sensible)",
                    "Consentimiento",
                    "Mientras exista la cuenta; borrado al eliminarla",
                  ],
                  [
                    "Alergias / restricciones",
                    "Seguridad alimentaria — restricción dura (dato sensible)",
                    "Consentimiento",
                    "Mientras exista la cuenta; borrado al eliminarla",
                  ],
                  [
                    "Preferencias y favoritos",
                    "Personalizar propuestas (dato sensible)",
                    "Consentimiento",
                    "Mientras exista la cuenta; borrado al eliminarla",
                  ],
                  [
                    "Equipamiento del hogar",
                    "Filtrar recetas por equipo disponible",
                    "Ejecución del contrato",
                    "Mientras exista la cuenta",
                  ],
                  [
                    "Planes, listas y feedback",
                    "Función principal del producto",
                    "Ejecución del contrato",
                    "Mientras exista la cuenta; borrado al eliminarla",
                  ],
                  [
                    "Contexto pseudonimizado a OpenAI",
                    "Proponer recetas candidatas",
                    "Consentimiento específico (opt-in)",
                    "No se conserva más allá de lo necesario; sin PII",
                  ],
                  [
                    "Consumo de IA (UsageLedger, cloud)",
                    "Cuotas y control de coste",
                    "Interés legítimo / contrato",
                    "Agregado; se anonimiza al eliminar la cuenta",
                  ],
                  [
                    "Registros de auditoría (AuditLog)",
                    "Seguridad y rendición de cuentas",
                    "Interés legítimo / obligación legal",
                    "Retención acotada; se anonimiza al eliminar la cuenta",
                  ],
                  [
                    "Logs técnicos (errores, IP truncada)",
                    "Operación y seguridad",
                    "Interés legítimo",
                    "Retención corta y acotada",
                  ],
                ].map(([dato, finalidad, base, retencion]) => (
                  <tr key={dato} className="border-b border-border last:border-0">
                    <td className="px-4 py-2.5 align-top text-ink">{dato}</td>
                    <td className="px-4 py-2.5 align-top">{finalidad}</td>
                    <td className="px-4 py-2.5 align-top">{base}</td>
                    <td className="px-4 py-2.5 align-top">{retencion}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section>
          <h2 className="font-display text-display-sm text-ink">9. Derechos del usuario</h2>
          <p className="mt-3 text-ink-muted">
            El usuario puede, sobre sus datos: acceder, exportar, rectificar (editando su
            perfil), retirar el consentimiento de IA y <strong>eliminar su cuenta</strong>{" "}
            (borrado real o anonimización). En despliegues sujetos al RGPD u otras
            normativas, el operador debe atender además los derechos aplicables
            (oposición, limitación, portabilidad) según su rol de responsable del
            tratamiento.
          </p>
        </section>
      </div>
    </div>
  );
}
