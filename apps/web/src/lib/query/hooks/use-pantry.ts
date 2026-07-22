"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addPantryItem,
  deletePantryItem,
  listIngredients,
  listPantry,
  updatePantryItem,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/query/keys";
import type { PantryItemCreate, PantryItemUpdate, Uuid } from "@/lib/api/types";

export function usePantryQuery(householdId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.pantry(householdId ?? ""),
    queryFn: () => listPantry(householdId as string),
    enabled: Boolean(householdId),
  });
}

export function useAddPantryItemMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PantryItemCreate) => addPantryItem(householdId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.pantry(householdId) }),
  });
}

export function useUpdatePantryItemMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, body }: { itemId: Uuid; body: PantryItemUpdate }) =>
      updatePantryItem(householdId, itemId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.pantry(householdId) }),
  });
}

export function useDeletePantryItemMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (itemId: Uuid) => deletePantryItem(householdId, itemId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.pantry(householdId) }),
  });
}

/** Ingredient autocomplete suggestions. Kept lightly cached; only enabled once the user types. */
export function useIngredientsQuery(search: string) {
  const trimmed = search.trim();
  return useQuery({
    queryKey: queryKeys.ingredients(trimmed),
    queryFn: () => listIngredients(trimmed),
    enabled: trimmed.length >= 2,
    staleTime: 5 * 60_000,
  });
}
