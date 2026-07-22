"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  commitAdminImport,
  createAdminImport,
  getAdminImport,
  listAdminImports,
  listAdminSources,
  rollbackAdminImport,
} from "@/lib/api/endpoints";
import { ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/auth-context";
import { queryKeys } from "@/lib/query/keys";
import type { CreateAdminImportInput, Uuid } from "@/lib/api/types";

/**
 * `GET /me` doesn't expose an `is_admin` flag on this API — the only signal
 * is whether an admin-only endpoint 403s. `GET /admin/sources` doubles as
 * that probe (and, on `/admin/fuentes`, as the real data query): every
 * consumer shares the same query key, so this never fires more than one
 * network request per cache window.
 */
export function useAdminSourcesQuery() {
  const { isAuthenticated } = useAuth();
  return useQuery({
    queryKey: queryKeys.adminSources(),
    queryFn: listAdminSources,
    enabled: isAuthenticated,
    retry: false,
  });
}

export interface AdminAccessState {
  /** True once the admin probe has confirmed access. */
  isAdmin: boolean;
  /** True if the probe came back 403 — an authenticated but non-admin user. */
  isForbidden: boolean;
  /** True for any other failure (network, 5xx) — distinct from "not an admin". */
  isError: boolean;
  isLoading: boolean;
}

/** Derives admin access from the `/admin/sources` probe for gating pages and nav. */
export function useIsAdminQuery(): AdminAccessState {
  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const sourcesQuery = useAdminSourcesQuery();

  const isForbidden = sourcesQuery.error instanceof ApiError && sourcesQuery.error.status === 403;

  return {
    isAdmin: isAuthenticated && sourcesQuery.isSuccess,
    isForbidden,
    isError: sourcesQuery.isError && !isForbidden,
    isLoading: authLoading || (isAuthenticated && sourcesQuery.isLoading),
  };
}

export function useAdminImportsQuery() {
  const { isAdmin } = useIsAdminQuery();
  return useQuery({
    queryKey: queryKeys.adminImports(),
    queryFn: listAdminImports,
    enabled: isAdmin,
  });
}

export function useAdminImportQuery(importId: string | null | undefined) {
  const { isAdmin } = useIsAdminQuery();
  return useQuery({
    queryKey: queryKeys.adminImport(importId ?? ""),
    queryFn: () => getAdminImport(importId as string),
    enabled: isAdmin && Boolean(importId),
  });
}

export function useCreateAdminImportMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateAdminImportInput) => createAdminImport(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.adminImports() }),
  });
}

export function useCommitAdminImportMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (importId: Uuid) => commitAdminImport(importId),
    onSuccess: (_data, importId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.adminImports() });
      queryClient.invalidateQueries({ queryKey: queryKeys.adminImport(importId) });
    },
  });
}

/**
 * Runs the full "confirm and apply" flow the API expects: re-upload the same
 * file with `dry_run: false` (creating a new, validated import batch), then
 * immediately commit that batch. Returns the final, committed record.
 */
export function useConfirmAndImportMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: Omit<CreateAdminImportInput, "dry_run">) => {
      const validated = await createAdminImport({ ...input, dry_run: false });
      return commitAdminImport(validated.id);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.adminImports() }),
  });
}

export function useRollbackAdminImportMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (importId: Uuid) => rollbackAdminImport(importId),
    onSuccess: (_data, importId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.adminImports() });
      queryClient.invalidateQueries({ queryKey: queryKeys.adminImport(importId) });
    },
  });
}
