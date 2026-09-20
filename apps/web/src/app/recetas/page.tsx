"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { MEAL_TYPE_LABELS, MEAL_TYPE_ORDER } from "@/lib/domain/labels";
import { useRecipesQuery } from "@/lib/query/hooks/use-catalog";
import type { MealType, RecipeSummary } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Skeleton } from "@/components/ui/Skeleton";

const PAGE_SIZE = 24;

/** Total hands-on + cooking time, e.g. "≈ 35 min". Null when the recipe declares neither. */
function totalMinutes(recipe: RecipeSummary): number | null {
  const prep = recipe.preparation_minutes ?? 0;
  const cook = recipe.cooking_minutes ?? 0;
  const total = prep + cook;
  return total > 0 ? total : null;
}

function RecipeCard({ recipe }: { recipe: RecipeSummary }) {
  const minutes = totalMinutes(recipe);
  const meals = recipe.meal_types.filter((m): m is MealType => m in MEAL_TYPE_LABELS);
  return (
    <li>
      <Link
        href={`/recetas/${recipe.id}`}
        className="flex h-full flex-col gap-3 rounded-lg border border-border bg-surface p-4 transition-colors hover:border-primary hover:bg-bg-subtle"
      >
        <div className="flex flex-col gap-1.5">
          <h2 className="font-display text-display-sm text-ink text-balance">{recipe.title}</h2>
          {recipe.description ? (
            <p className="line-clamp-2 text-sm text-ink-muted">{recipe.description}</p>
          ) : null}
        </div>

        {meals.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {meals.map((meal) => (
              <Badge key={meal} tone="neutral">
                {MEAL_TYPE_LABELS[meal]}
              </Badge>
            ))}
          </div>
        ) : null}

        <div className="mt-auto flex items-center gap-3 text-xs text-ink-faint">
          <span>
            {recipe.servings} {recipe.servings === 1 ? "ración" : "raciones"}
          </span>
          {minutes ? <span>≈ {minutes} min</span> : null}
        </div>
      </Link>
    </li>
  );
}

export default function RecetasPage() {
  const router = useRouter();
  const { isAuthenticated, isLoading: authLoading } = useAuth();

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [authLoading, isAuthenticated, router]);

  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [mealType, setMealType] = useState<MealType | "">("");
  const [page, setPage] = useState(1);

  // Debounce the free-text search so we don't fire a request per keystroke.
  useEffect(() => {
    const handle = setTimeout(() => {
      setSearch(searchInput.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const recipesQuery = useRecipesQuery({ search, mealType, page, size: PAGE_SIZE });
  const totalPages = recipesQuery.data
    ? Math.max(1, Math.ceil(recipesQuery.data.count / PAGE_SIZE))
    : 1;

  if (authLoading || !isAuthenticated) {
    return null;
  }

  const items = recipesQuery.data?.items ?? [];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Recetas</h1>
        <p className="mt-2 text-ink-muted">
          Explora el recetario que usa el planificador. Encuentra ideas por tipo de comida
          antes de configurar tu hogar.
        </p>
      </div>

      <div className="flex flex-col gap-4">
        <Input
          label="Buscar receta"
          placeholder="p. ej. lentejas, tortilla, ensalada…"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
        />

        <div className="flex flex-wrap gap-2" role="group" aria-label="Filtrar por tipo de comida">
          <Button
            type="button"
            size="sm"
            variant={mealType === "" ? "primary" : "outline"}
            onClick={() => {
              setMealType("");
              setPage(1);
            }}
          >
            Todas
          </Button>
          {MEAL_TYPE_ORDER.map((meal) => (
            <Button
              key={meal}
              type="button"
              size="sm"
              variant={mealType === meal ? "primary" : "outline"}
              onClick={() => {
                setMealType(meal);
                setPage(1);
              }}
            >
              {MEAL_TYPE_LABELS[meal]}
            </Button>
          ))}
        </div>
      </div>

      {recipesQuery.isLoading ? (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <li key={i}>
              <Skeleton className="h-40 w-full" />
            </li>
          ))}
        </ul>
      ) : recipesQuery.isError ? (
        <Alert tone="error">No se pudieron cargar las recetas. Comprueba tu conexión.</Alert>
      ) : items.length === 0 ? (
        <Card>
          <CardContent>
            <Alert tone="info">
              {search || mealType
                ? "Ninguna receta coincide con tu búsqueda. Prueba con otros términos o quita el filtro."
                : "Todavía no hay recetas disponibles."}
            </Alert>
          </CardContent>
        </Card>
      ) : (
        <>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((recipe) => (
              <RecipeCard key={recipe.id} recipe={recipe} />
            ))}
          </ul>

          <div className="flex items-center justify-between gap-3 pt-1">
            <p className="text-xs text-ink-faint">
              {recipesQuery.data?.count} receta{recipesQuery.data?.count === 1 ? "" : "s"}
            </p>
            {totalPages > 1 ? (
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((current) => Math.max(1, current - 1))}
                >
                  Anterior
                </Button>
                <span className="text-xs text-ink-muted">
                  Página {page} de {totalPages}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                >
                  Siguiente
                </Button>
              </div>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
