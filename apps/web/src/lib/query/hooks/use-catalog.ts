"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import {
  getRecipe,
  listPriceProviders,
  listRetailers,
  listStorePrices,
  listStores,
} from "@/lib/api/endpoints";
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

export function usePriceProvidersQuery() {
  return useQuery({
    queryKey: queryKeys.priceProviders(),
    queryFn: listPriceProviders,
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

export interface UseStorePricesOptions {
  search?: string;
  page?: number;
  size?: number;
}

/** Real, ODbL-attributed prices for one store — the read-only "Precios reales" viewer. */
export function useStorePricesQuery(
  retailerId: string | null | undefined,
  storeId: string | null | undefined,
  { search = "", page = 1, size = 20 }: UseStorePricesOptions = {},
) {
  return useQuery({
    queryKey: queryKeys.storePrices(retailerId ?? "", storeId ?? "", page, search),
    queryFn: () => listStorePrices(retailerId as string, storeId as string, { search, page, size }),
    enabled: Boolean(retailerId) && Boolean(storeId),
    retry: false,
    // Keep the previous page's rows visible while a new page/search fetches.
    placeholderData: keepPreviousData,
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
