"use client";

import { useParams, useSearchParams } from "next/navigation";

import { setFeedbackStatus } from "@/lib/domain/feedback-log";
import { useFeedbackLog } from "@/lib/domain/use-feedback-log";
import { EQUIPMENT_LABELS } from "@/lib/domain/labels";
import { useRecipeQuery } from "@/lib/query/hooks/use-catalog";
import { useFavoriteRecipeMutation, useRecipeFeedbackMutation } from "@/lib/query/hooks/use-plans";
import type { EquipmentCode } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";

export default function RecetaDetallePage() {
  const params = useParams<{ recipeId: string }>();
  const searchParams = useSearchParams();
  const householdId = searchParams.get("householdId") ?? "";
  const { showToast } = useToast();

  const recipeQuery = useRecipeQuery(params.recipeId);
  const feedbackLog = useFeedbackLog();
  const currentStatus = feedbackLog.find((entry) => entry.recipeId === params.recipeId)?.status ?? null;

  const favoriteMutation = useFavoriteRecipeMutation(householdId);
  const feedbackMutation = useRecipeFeedbackMutation(householdId);

  if (recipeQuery.isLoading) {
    return (
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (recipeQuery.isError || !recipeQuery.data) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
        <Alert tone="error">No se pudo cargar la receta.</Alert>
      </div>
    );
  }

  const recipe = recipeQuery.data;

  const toggleFavorite = async () => {
    if (!householdId) {
      showToast({ tone: "warning", title: "Abre la receta desde un plan para poder marcarla" });
      return;
    }
    const nextFavorited = currentStatus !== "favorite";
    try {
      await favoriteMutation.mutateAsync({ recipeId: recipe.id, favorite: nextFavorited });
      setFeedbackStatus(recipe.id, recipe.title, householdId, nextFavorited ? "favorite" : null);
      showToast({ tone: "success", title: nextFavorited ? "Añadida a favoritos" : "Quitada de favoritos" });
    } catch {
      showToast({ tone: "error", title: "No se pudo actualizar el favorito" });
    }
  };

  const reject = async () => {
    if (!householdId) {
      showToast({ tone: "warning", title: "Abre la receta desde un plan para poder rechazarla" });
      return;
    }
    try {
      await feedbackMutation.mutateAsync({ recipeId: recipe.id, body: { sentiment: "reject" } });
      setFeedbackStatus(recipe.id, recipe.title, householdId, "rejected");
      showToast({ tone: "info", title: "No volveremos a proponer esta receta" });
    } catch {
      showToast({ tone: "error", title: "No se pudo rechazar la receta" });
    }
  };

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="font-display text-display-lg text-ink">{recipe.title}</h1>
          {recipe.description ? <p className="mt-2 text-ink-muted">{recipe.description}</p> : null}
        </div>
        <button
          type="button"
          onClick={toggleFavorite}
          aria-pressed={currentStatus === "favorite"}
          aria-label={currentStatus === "favorite" ? "Quitar de favoritos" : "Añadir a favoritos"}
          className="shrink-0 rounded-full p-2 text-2xl text-accent-strong transition-colors hover:bg-accent-soft"
        >
          {currentStatus === "favorite" ? "♥" : "♡"}
        </button>
      </div>

      <div className="flex flex-wrap gap-2">
        <Badge tone="neutral">{recipe.servings} ración(es)</Badge>
        {recipe.preparation_minutes != null ? (
          <Badge tone="neutral">Prep. {recipe.preparation_minutes} min</Badge>
        ) : null}
        {recipe.cooking_minutes != null ? <Badge tone="neutral">Cocción {recipe.cooking_minutes} min</Badge> : null}
        {recipe.cuisine ? <Badge tone="info">{recipe.cuisine}</Badge> : null}
        {recipe.preference_tags.map((tag) => (
          <Badge key={tag} tone="accent">
            {tag}
          </Badge>
        ))}
      </div>

      {recipe.allergens.length > 0 ? (
        <Alert tone="warning" title="Contiene alérgenos declarados">
          {recipe.allergens.join(", ")}. Comprueba siempre la etiqueta del producto: esta
          información es orientativa y no sustituye el consejo de un profesional sanitario.
        </Alert>
      ) : (
        <Alert tone="info">
          Sin alérgenos declarados en esta receta. Comprueba siempre la etiqueta del producto:
          esta información es orientativa y no sustituye el consejo de un profesional sanitario.
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Ingredientes</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="flex flex-col gap-2">
            {recipe.ingredients.map((ingredient) => (
              <li
                key={ingredient.canonical_name}
                className="flex items-center justify-between border-b border-border pb-2 text-sm last:border-b-0 last:pb-0"
              >
                <span className="text-ink">
                  {ingredient.display_name}
                  {ingredient.optional ? <span className="text-ink-faint"> (opcional)</span> : null}
                </span>
                <span className="text-ink-muted">
                  {ingredient.quantity} {ingredient.unit}
                </span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Elaboración</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="flex flex-col gap-3">
            {recipe.steps
              .slice()
              .sort((a, b) => a.position - b.position)
              .map((step) => (
                <li key={step.position} className="flex gap-3 text-sm">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-soft text-xs font-semibold text-primary">
                    {step.position}
                  </span>
                  <span className="text-ink">{step.instruction}</span>
                </li>
              ))}
          </ol>
        </CardContent>
      </Card>

      {recipe.required_equipment.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Equipamiento necesario</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {recipe.required_equipment.map((code) => (
                <Badge key={code} tone="neutral">
                  {EQUIPMENT_LABELS[code as EquipmentCode] ?? code}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {recipe.nutrition ? (
        <Card>
          <CardHeader>
            <CardTitle>Información nutricional</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
              {Object.entries(recipe.nutrition).map(([key, value]) => (
                <div key={key}>
                  <dt className="text-ink-muted">{key}</dt>
                  <dd className="font-medium text-ink">{value}</dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
      ) : null}

      <div className="flex justify-end">
        <Button
          type="button"
          variant="ghost"
          loading={feedbackMutation.isPending}
          onClick={reject}
          disabled={currentStatus === "rejected"}
        >
          {currentStatus === "rejected" ? "Rechazada" : "No volver a proponer esta receta"}
        </Button>
      </div>
    </div>
  );
}
