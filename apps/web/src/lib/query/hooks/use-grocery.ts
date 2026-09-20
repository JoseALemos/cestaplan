"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addGroceryItem,
  getGroceryList,
  searchGroceryProducts,
  substituteGroceryItem,
} from "@/lib/api/endpoints";
import { getListSnapshot, saveListSnapshot } from "@/lib/offline/grocery-db";
import { queryKeys } from "@/lib/query/keys";
import type { GroceryItemIn, SubstituteRequest, Uuid } from "@/lib/api/types";

export function useGroceryListQuery(mealPlanId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.groceryList(mealPlanId ?? ""),
    queryFn: async () => {
      const id = mealPlanId as string;
      try {
        const list = await getGroceryList(id);
        // Cache the fresh list so the screen can open cold with no signal (in-store).
        void saveListSnapshot(id, list);
        return list;
      } catch (err) {
        // Offline / fetch failed: fall back to the last saved snapshot if we have one.
        // The checklist overlay still shows the right bought state on top of it.
        const cached = await getListSnapshot(id);
        if (cached) return cached;
        throw err;
      }
    },
    enabled: Boolean(mealPlanId),
    // A snapshot fallback shouldn't be hammered with retries when offline.
    retry: false,
  });
}

export function useAddGroceryItemMutation(mealPlanId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: GroceryItemIn) => addGroceryItem(mealPlanId, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.groceryList(mealPlanId) }),
  });
}

/** Live product search for the substitute picker; only runs for a query of >= 2 chars. */
export function useProductSearchQuery(mealPlanId: string, search: string, enabled: boolean) {
  const term = search.trim();
  return useQuery({
    queryKey: queryKeys.groceryProductSearch(mealPlanId, term),
    queryFn: () => searchGroceryProducts(mealPlanId, term),
    enabled: enabled && term.length >= 2,
    retry: false,
  });
}

export function useSubstituteGroceryItemMutation(mealPlanId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, body }: { itemId: Uuid; body: SubstituteRequest }) =>
      substituteGroceryItem(mealPlanId, itemId, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.groceryList(mealPlanId) }),
  });
}
