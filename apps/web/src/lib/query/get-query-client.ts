import { QueryClient, isServer } from "@tanstack/react-query";

function makeQueryClient(): QueryClient {
  return new QueryClient({
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
