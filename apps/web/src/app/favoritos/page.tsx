"use client";

import Link from "next/link";

import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { setFeedbackStatus } from "@/lib/domain/feedback-log";
import {
  useClearFeedbackMutation,
  useFavoriteRecipeMutation,
  useFavoritesQuery,
  useFeedbackQuery,
} from "@/lib/query/hooks/use-plans";
import { formatDateTime } from "@/lib/utils/format";
import type { RecipeFeedbackListItem } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";

const REJECTED_SENTIMENTS = new Set(["reject", "no_show"]);

function sentimentLabel(sentiment: RecipeFeedbackListItem["sentiment"]): string {
  return sentiment === "no_show" ? "No volver a mostrar" : "Rechazada";
}

export default function FavoritosPage() {
  const [householdId] = useCurrentHouseholdId();
  const { showToast } = useToast();

  const favoritesQuery = useFavoritesQuery(householdId);
  const feedbackQuery = useFeedbackQuery(householdId);
  const favoriteMutation = useFavoriteRecipeMutation(householdId ?? "");
  const clearFeedbackMutation = useClearFeedbackMutation(householdId ?? "");

  if (!householdId) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <Alert tone="info" title="Selecciona un hogar">
          Elige un hogar para ver sus recetas favoritas y rechazadas.{" "}
          <Link href="/households" className="font-medium underline">
            Ir a hogares
          </Link>
        </Alert>
      </div>
    );
  }

  const rejected = (feedbackQuery.data ?? []).filter((entry) =>
    REJECTED_SENTIMENTS.has(entry.sentiment),
  );

  const removeFavorite = async (recipeId: string, title: string) => {
    try {
      await favoriteMutation.mutateAsync({ recipeId, favorite: false });
      setFeedbackStatus(recipeId, title, householdId, null);
      showToast({ tone: "success", title: "Quitada de favoritos" });
    } catch {
      showToast({ tone: "error", title: "No se pudo quitar de favoritos" });
    }
  };

  const clearRejection = async (recipeId: string, title: string) => {
    try {
      await clearFeedbackMutation.mutateAsync(recipeId);
      setFeedbackStatus(recipeId, title, householdId, null);
      showToast({ tone: "success", title: "Ya puede volver a proponerse" });
    } catch {
      showToast({ tone: "error", title: "No se pudo actualizar" });
    }
  };

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Favoritos y rechazados</h1>
        <p className="mt-2 text-ink-muted">
          Recetas que has marcado desde un plan o desde su ficha, para este hogar.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Favoritos</CardTitle>
          <CardDescription>Recetas que quieres volver a ver propuestas.</CardDescription>
        </CardHeader>
        <CardContent>
          {favoritesQuery.isLoading ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : favoritesQuery.isError ? (
            <Alert tone="error">No se pudieron cargar los favoritos.</Alert>
          ) : (favoritesQuery.data ?? []).length === 0 ? (
            <Alert tone="info">Todavía no has marcado ninguna receta como favorita.</Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {(favoritesQuery.data ?? []).map((entry) => (
                <li
                  key={entry.recipe_id}
                  className="flex items-center justify-between gap-3 rounded-md border border-border px-4 py-3"
                >
                  <div>
                    <Link
                      href={`/recetas/${entry.recipe_id}?householdId=${householdId}`}
                      className="text-sm font-medium text-ink hover:text-primary"
                    >
                      {entry.title}
                    </Link>
                    <p className="text-xs text-ink-faint">
                      Marcada el {formatDateTime(entry.favorited_at)}
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    loading={favoriteMutation.isPending}
                    onClick={() => removeFavorite(entry.recipe_id, entry.title)}
                  >
                    Quitar
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Rechazadas / No volver a mostrar</CardTitle>
          <CardDescription>No volverán a proponerse en tus próximos planes.</CardDescription>
        </CardHeader>
        <CardContent>
          {feedbackQuery.isLoading ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : feedbackQuery.isError ? (
            <Alert tone="error">No se pudieron cargar las recetas rechazadas.</Alert>
          ) : rejected.length === 0 ? (
            <Alert tone="info">No has rechazado ninguna receta todavía.</Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {rejected.map((entry) => (
                <li
                  key={entry.recipe_id}
                  className="flex items-center justify-between gap-3 rounded-md border border-border px-4 py-3"
                >
                  <div className="flex items-center gap-2">
                    <div>
                      <Link
                        href={`/recetas/${entry.recipe_id}?householdId=${householdId}`}
                        className="text-sm font-medium text-ink hover:text-primary"
                      >
                        {entry.title}
                      </Link>
                      <p className="text-xs text-ink-faint">
                        Actualizada el {formatDateTime(entry.updated_at)}
                      </p>
                    </div>
                    <Badge tone="error">{sentimentLabel(entry.sentiment)}</Badge>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    loading={clearFeedbackMutation.isPending}
                    onClick={() => clearRejection(entry.recipe_id, entry.title)}
                  >
                    Quitar
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
