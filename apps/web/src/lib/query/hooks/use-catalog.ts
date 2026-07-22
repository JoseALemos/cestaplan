"use client";

import { useQuery } from "@tanstack/react-query";

import { getRecipe, listRetailers, listStores } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/query/keys";

/**
 * Retailers/stores are documented in the FASE 3.5 brief but not present in
 * the live `openapi.json` yet. `retry: false` + the caller's own error UI
 * turns the expected 404 today into a calm "todavía no disponible" state
 * instead of a silent retry storm.
 */
export function useRetailersQuery() {
  return useQuery({
    queryKey: queryKeys.retailers(),
    queryFn: listRetailers,
    retry: false,
  });
}

export function useStoresQuery(retailerId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.stores(retailerId ?? ""),
    queryFn: () => listStores(retailerId as string),
    enabled: Boolean(retailerId),
    retry: false,
  });
}

export function useRecipeQuery(recipeId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.recipe(recipeId ?? ""),
    queryFn: () => getRecipe(recipeId as string),
    enabled: Boolean(recipeId),
    retry: false,
  });
}
