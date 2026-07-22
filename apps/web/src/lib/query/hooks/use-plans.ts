"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  favoriteRecipe,
  generatePlan,
  getPlan,
  getRunStatus,
  regenerateMeal,
  regeneratePlan,
  submitFeedback,
  unfavoriteRecipe,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/query/keys";
import type { FeedbackRequest, GenerateRequest, Uuid } from "@/lib/api/types";

const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled"]);

export function useGeneratePlanMutation() {
  return useMutation({ mutationFn: (body: GenerateRequest) => generatePlan(body) });
}

/** Polls the optimization run with capped exponential backoff (1.5s → 8s), stopping once terminal. */
export function useRunStatusQuery(runId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.runStatus(runId ?? ""),
    queryFn: () => getRunStatus(runId as string),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status && TERMINAL_RUN_STATUSES.has(status)) return false;
      const attempt = query.state.dataUpdateCount;
      return Math.min(1500 * 1.5 ** attempt, 8000);
    },
  });
}

export function usePlanQuery(mealPlanId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.plan(mealPlanId ?? ""),
    queryFn: () => getPlan(mealPlanId as string),
    enabled: Boolean(mealPlanId),
  });
}

export function useRegeneratePlanMutation(mealPlanId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => regeneratePlan(mealPlanId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.plan(mealPlanId) }),
  });
}

export function useRegenerateMealMutation(mealPlanId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (plannedMealId: Uuid) => regenerateMeal(mealPlanId, plannedMealId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.plan(mealPlanId) }),
  });
}

export function useFavoriteRecipeMutation(householdId: string) {
  return useMutation({
    mutationFn: ({ recipeId, favorite }: { recipeId: Uuid; favorite: boolean }) =>
      favorite ? favoriteRecipe(recipeId, householdId) : unfavoriteRecipe(recipeId, householdId),
  });
}

export function useRecipeFeedbackMutation(householdId: string, mealPlanId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ recipeId, body }: { recipeId: Uuid; body: FeedbackRequest }) =>
      submitFeedback(recipeId, householdId, body),
    onSuccess: () => {
      if (mealPlanId) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.plan(mealPlanId) });
      }
    },
  });
}
