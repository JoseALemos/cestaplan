"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { createContext, use, useMemo } from "react";

import { ApiError } from "@/lib/api/client";
import { getMe } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/query/keys";
import type { UserResponse } from "@/lib/api/types";

interface AuthContextValue {
  user: UserResponse | undefined;
  isLoading: boolean;
  isAuthenticated: boolean;
  refetch: () => Promise<unknown>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: queryKeys.me(),
    queryFn: getMe,
    retry: false,
    staleTime: 5 * 60 * 1000,
    // A 401 just means "not logged in" — not a fetch failure to retry/alert on.
    throwOnError: false,
  });

  const value = useMemo<AuthContextValue>(
    () => ({
      user: query.data,
      isLoading: query.isLoading,
      isAuthenticated: Boolean(query.data) && !query.isError,
      refetch: query.refetch,
    }),
    [query.data, query.isLoading, query.isError, query.refetch],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthContextValue {
  const context = use(AuthContext);
  if (!context) {
    throw new Error("useAuth debe usarse dentro de <AuthProvider>");
  }
  return context;
}

/** Invalidates the cached session after login/register/logout so `useAuth` reflects it immediately. */
export function useInvalidateAuth() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: queryKeys.me() });
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}
