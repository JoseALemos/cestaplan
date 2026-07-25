"use client";

import Link from "next/link";
import { useState } from "react";

import { setFeedbackStatus } from "@/lib/domain/feedback-log";
import { useFeedbackLog } from "@/lib/domain/use-feedback-log";
import {
  useFavoriteRecipeMutation,
  useRecipeFeedbackMutation,
  useRegenerateMealMutation,
} from "@/lib/query/hooks/use-plans";
import { formatMoney } from "@/lib/utils/format";
import type { PlannedMeal } from "@/lib/api/types";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useToast } from "@/components/ui/Toast";

export interface MealCardProps {
  meal: PlannedMeal;
  householdId: string;
  mealPlanId: string;
  currency: string;
  onRegenerateStarted: (runId: string) => void;
}

export function MealCard({
  meal,
  householdId,
  mealPlanId,
  currency,
  onRegenerateStarted,
}: MealCardProps) {
  const { showToast } = useToast();
  const [explanationOpen, setExplanationOpen] = useState(false);
  const feedbackLog = useFeedbackLog();
  const currentStatus = feedbackLog.find((entry) => entry.recipeId === meal.recipe_id)?.status ?? null;

  const favoriteMutation = useFavoriteRecipeMutation(householdId);
  const feedbackMutation = useRecipeFeedbackMutation(householdId, mealPlanId);
  const regenerateMealMutation = useRegenerateMealMutation(mealPlanId);

  // A per-dish cost of 0 / empty means "no price" (real food is never free), so
  // show "Sin precio" instead of a misleading "0,00 €".
  const imputable = meal.cost.imputable;
  const hasPrice = imputable !== null && imputable !== "" && Number(imputable) > 0;

  const toggleFavorite = async () => {
    const nextFavorited = currentStatus !== "favorite";
    try {
      await favoriteMutation.mutateAsync({ recipeId: meal.recipe_id, favorite: nextFavorited });
      setFeedbackStatus(meal.recipe_id, meal.title, householdId, nextFavorited ? "favorite" : null);
      showToast({
        tone: "success",
        title: nextFavorited ? "Añadido a favoritos" : "Quitado de favoritos",
      });
    } catch {
      showToast({ tone: "error", title: "No se pudo actualizar el favorito" });
    }
  };

  const reject = async () => {
    try {
      await feedbackMutation.mutateAsync({ recipeId: meal.recipe_id, body: { sentiment: "reject" } });
      setFeedbackStatus(meal.recipe_id, meal.title, householdId, "rejected");
      showToast({ tone: "info", title: "No volveremos a proponer esta receta" });
      const accepted = await regenerateMealMutation.mutateAsync(meal.id);
      onRegenerateStarted(accepted.optimization_run_id);
    } catch {
      showToast({ tone: "error", title: "No se pudo rechazar la receta" });
    }
  };

  const regenerate = async () => {
    try {
      const accepted = await regenerateMealMutation.mutateAsync(meal.id);
      onRegenerateStarted(accepted.optimization_run_id);
    } catch {
      showToast({ tone: "error", title: "No se pudo regenerar este plato" });
    }
  };

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <Link
            href={`/recetas/${meal.recipe_id}?householdId=${householdId}`}
            className="font-display text-display-sm text-ink hover:text-primary"
          >
            {meal.title}
          </Link>
          <p className="mt-0.5 text-sm text-ink-muted">
            {meal.servings} ración(es) ·{" "}
            {hasPrice ? formatMoney(meal.cost.imputable, currency) : "Sin precio"}
          </p>
        </div>
        <button
          type="button"
          onClick={toggleFavorite}
          aria-pressed={currentStatus === "favorite"}
          aria-label={currentStatus === "favorite" ? "Quitar de favoritos" : "Añadir a favoritos"}
          className="shrink-0 rounded-full p-2 text-lg text-accent-strong transition-colors hover:bg-accent-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-focus-ring)]"
        >
          {currentStatus === "favorite" ? "♥" : "♡"}
        </button>
      </div>

      {meal.nutrition ? (
        <div className="flex flex-wrap gap-1.5">
          {meal.nutrition.kcal ? <Badge tone="neutral">{meal.nutrition.kcal} kcal</Badge> : null}
          {meal.nutrition.protein_g ? <Badge tone="neutral">{meal.nutrition.protein_g} g prot.</Badge> : null}
          {!meal.nutrition_complete ? <Badge tone="warning">Nutrición incompleta</Badge> : null}
        </div>
      ) : null}

      {meal.explanation ? (
        <div>
          <button
            type="button"
            onClick={() => setExplanationOpen((open) => !open)}
            aria-expanded={explanationOpen}
            className="text-sm font-medium text-primary hover:underline"
          >
            {explanationOpen ? "Ocultar" : "¿Por qué este plato?"}
          </button>
          {explanationOpen ? (
            <p className="mt-1.5 text-sm text-ink-muted">{meal.explanation}</p>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-wrap gap-2 border-t border-border pt-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={regenerateMealMutation.isPending}
          onClick={regenerate}
        >
          Regenerar
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          loading={feedbackMutation.isPending}
          onClick={reject}
          disabled={currentStatus === "rejected"}
        >
          {currentStatus === "rejected" ? "Rechazada" : "No volver a mostrar"}
        </Button>
      </div>
    </li>
  );
}
