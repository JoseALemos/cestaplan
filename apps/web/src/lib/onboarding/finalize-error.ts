/**
 * User-facing message for a failure while finalizing onboarding (create household → members →
 * equipment → plan). Pure over the HTTP status (or `null` for a network/non-API error) so it is
 * trivially testable and carries no dependency on the fetch client. Never exposes tokens, ids or
 * server internals.
 *
 * The 403 case is the important one: onboarding finalization POSTs are CSRF-protected, and when
 * the web and API are served from DIFFERENT registrable domains (e.g. two `*.up.railway.app`
 * services, which are cross-site per the Public Suffix List) the browser does not return the
 * session/CSRF cookies, so the double-submit CSRF check rejects the write with 403. The message
 * explains the likely cause and the recovery without leaking anything sensitive.
 */
export function finalizeErrorMessage(status: number | null): string {
  if (status === 401) {
    return "Necesitas iniciar sesión para crear un hogar y generar un plan.";
  }
  if (status === 403) {
    return (
      "No pudimos verificar tu sesión al finalizar. Suele ocurrir cuando la web y la API " +
      "están en dominios distintos y el navegador bloquea las cookies entre sitios. Cierra " +
      "sesión y vuelve a entrar; si persiste, la app y la API deben servirse bajo el mismo " +
      "dominio."
    );
  }
  if (status === 422) {
    return "Alguno de los datos no cumple lo que exige la API. Revisa los pasos anteriores.";
  }
  if (status !== null) {
    return `La API respondió con un error (${status}). Puedes reintentar: lo que ya se guardó no se repetirá.`;
  }
  return "No se pudo conectar con la API. Comprueba tu conexión y reintenta.";
}

/** Whether the message should offer a "go to login" affordance. */
export function finalizeErrorNeedsLogin(status: number | null): boolean {
  return status === 401 || status === 403;
}
