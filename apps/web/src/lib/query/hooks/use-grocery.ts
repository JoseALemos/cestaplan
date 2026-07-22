"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { addGroceryItem, getGroceryList, substituteGroceryItem } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/query/keys";
import type { GroceryItemIn, SubstituteRequest, Uuid } from "@/lib/api/types";

export function useGroceryListQuery(mealPlanId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.groceryList(mealPlanId ?? ""),
    queryFn: () => getGroceryList(mealPlanId as string),
    enabled: Boolean(mealPlanId),
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

export function useSubstituteGroceryItemMutation(mealPlanId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, body }: { itemId: Uuid; body: SubstituteRequest }) =>
      substituteGroceryItem(mealPlanId, itemId, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.groceryList(mealPlanId) }),
  });
}
