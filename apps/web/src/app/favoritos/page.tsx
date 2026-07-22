"use client";

import Link from "next/link";

import { setFeedbackStatus } from "@/lib/domain/feedback-log";
import { useFeedbackLog } from "@/lib/domain/use-feedback-log";
import { formatDateTime } from "@/lib/utils/format";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";

export default function FavoritosPage() {
  const entries = useFeedbackLog();
  const favorites = entries.filter((entry) => entry.status === "favorite");
  const rejected = entries.filter((entry) => entry.status === "rejected");

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Favoritos y rechazados</h1>
        <p className="mt-2 text-ink-muted">
          Recetas que has marcado desde un plan o desde su ficha. Se guardan en este dispositivo.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Favoritos</CardTitle>
          <CardDescription>Recetas que quieres volver a ver propuestas.</CardDescription>
        </CardHeader>
        <CardContent>
          {favorites.length === 0 ? (
            <Alert tone="info">Todavía no has marcado ninguna receta como favorita.</Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {favorites.map((entry) => (
                <li
                  key={entry.recipeId}
                  className="flex items-center justify-between gap-3 rounded-md border border-border px-4 py-3"
                >
                  <div>
                    <Link
                      href={`/recetas/${entry.recipeId}?householdId=${entry.householdId}`}
                      className="text-sm font-medium text-ink hover:text-primary"
                    >
                      {entry.title}
                    </Link>
                    <p className="text-xs text-ink-faint">Marcada el {formatDateTime(entry.updatedAt)}</p>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setFeedbackStatus(entry.recipeId, entry.title, entry.householdId, null)}
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
          <CardTitle>Rechazadas</CardTitle>
          <CardDescription>No volverán a proponerse en tus próximos planes.</CardDescription>
        </CardHeader>
        <CardContent>
          {rejected.length === 0 ? (
            <Alert tone="info">No has rechazado ninguna receta todavía.</Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {rejected.map((entry) => (
                <li
                  key={entry.recipeId}
                  className="flex items-center justify-between gap-3 rounded-md border border-border px-4 py-3"
                >
                  <div>
                    <Link
                      href={`/recetas/${entry.recipeId}?householdId=${entry.householdId}`}
                      className="text-sm font-medium text-ink hover:text-primary"
                    >
                      {entry.title}
                    </Link>
                    <p className="text-xs text-ink-faint">Rechazada el {formatDateTime(entry.updatedAt)}</p>
                  </div>
                  <Badge tone="error">Rechazada</Badge>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
