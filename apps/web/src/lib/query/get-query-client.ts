import { MutationCache, QueryCache, QueryClient, isServer } from "@tanstack/react-query";

import { ApiError, clearCsrfToken } from "@/lib/api/client";

// En estas rutas un 401 es esperable (usuario no logueado viendo contenido público): NO redirigir.
const PUBLIC_PATHS = new Set([
  "/",
  "/login",
  "/registro",
  "/recuperar-contrasena",
  "/privacidad",
]);

// Evita disparar varias navegaciones si varias queries fallan con 401 a la vez.
let redirectingToLogin = false;

/**
 * Ante un 401 en una ruta protegida (sesión caducada/inválida), lleva al login en vez de dejar la
 * UI cacheada en un estado "zombi" donde nada funciona. En rutas públicas un 401 es normal y se
 * ignora. Sólo actúa en el navegador.
 */
function handleUnauthorized(error: unknown): void {
  if (typeof window === "undefined") return;
  if (!(error instanceof ApiError) || error.status !== 401) return;
  if (redirectingToLogin) return;
  if (PUBLIC_PATHS.has(window.location.pathname)) return;
  redirectingToLogin = true;
  clearCsrfToken();
  window.location.assign("/login");
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({ onError: handleUnauthorized }),
    mutationCache: new MutationCache({ onError: handleUnauthorized }),
    defaultOptions: {
      queries: {
        // Avoid refetch storms on window focus for a mobile-first PWA where
        // users background/foreground the app constantly.
        refetchOnWindowFocus: false,
        staleTime: 30 * 1000,
        retry: 1,
      },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

/**
 * Returns a request-scoped QueryClient on the server (a new one per
 * request, per the TanStack Query App Router guidance) and a singleton in
 * the browser so cache survives client-side navigations.
 */
export function getQueryClient(): QueryClient {
  if (isServer) {
    return makeQueryClient();
  }
  if (!browserQueryClient) {
    browserQueryClient = makeQueryClient();
  }
  return browserQueryClient;
}
