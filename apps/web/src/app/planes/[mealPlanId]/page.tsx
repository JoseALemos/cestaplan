"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";

import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { MEAL_TYPE_ORDER } from "@/lib/domain/labels";
import { usePlanQuery, useRegeneratePlanMutation } from "@/lib/query/hooks/use-plans";
import { formatDateLong } from "@/lib/utils/format";
import type { PlannedMeal } from "@/lib/api/types";

import { MealCard } from "@/components/plan/MealCard";
import { NutritionSummaryPanel } from "@/components/plan/NutritionSummaryPanel";
import { PlanHeader } from "@/components/plan/PlanHeader";
import { Alert } from "@/components/ui/Alert";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";

function groupByDay(meals: PlannedMeal[]): { date: string; meals: PlannedMeal[] }[] {
  const byDate = new Map<string, PlannedMeal[]>();
  for (const meal of meals) {
    const bucket = byDate.get(meal.date) ?? [];
    bucket.push(meal);
    byDate.set(meal.date, bucket);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, dayMeals]) => ({
      date,
      meals: [...dayMeals].sort(
        (a, b) => MEAL_TYPE_ORDER.indexOf(a.meal_type) - MEAL_TYPE_ORDER.indexOf(b.meal_type),
      ),
    }));
}

export default function PlanPage() {
  const params = useParams<{ mealPlanId: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const mealPlanId = params.mealPlanId;
  const [currentHouseholdId] = useCurrentHouseholdId();
  const householdId = searchParams.get("householdId") ?? currentHouseholdId ?? "";

  const planQuery = usePlanQuery(mealPlanId);
  const regeneratePlanMutation = useRegeneratePlanMutation(mealPlanId);

  const goToEstado = (runId: string) => {
    router.push(`/planes/estado/${runId}?mealPlanId=${mealPlanId}&householdId=${householdId}`);
  };

  if (planQuery.isLoading) {
    return (
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (planQuery.isError || !planQuery.data) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <Alert tone="error">No se pudo cargar el plan. Comprueba tu conexión e inténtalo de nuevo.</Alert>
      </div>
    );
  }

  const plan = planQuery.data;
  const currency = plan.budget.currency;
  const days = groupByDay(plan.planned_meals);

  if (!householdId) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <Alert tone="warning" title="Falta el hogar">
          Abre este plan desde{" "}
          <Link href="/households" className="underline">
            tus hogares
          </Link>{" "}
          para poder marcar favoritos o rechazar recetas.
        </Alert>
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <PlanHeader
        plan={plan}
        regenerating={regeneratePlanMutation.isPending}
        onRegenerate={async () => {
          const accepted = await regeneratePlanMutation.mutateAsync();
          goToEstado(accepted.optimization_run_id);
        }}
      />

      {plan.nutrition_summary ? (
        <NutritionSummaryPanel summary={plan.nutrition_summary} />
      ) : days.length > 0 ? (
        <Alert tone="info" title="Objetivos nutricionales">
          Define objetivos de nutrición en los perfiles de tu hogar para ver aquí la energía y los
          macros del plan frente a tu meta.
        </Alert>
      ) : null}

      {days.length === 0 ? (
        <Card>
          <p className="text-sm text-ink-muted">Este plan todavía no tiene comidas asignadas.</p>
        </Card>
      ) : (
        days.map(({ date, meals }) => (
          <section key={date} className="flex flex-col gap-3">
            <h2 className="font-display text-display-sm capitalize text-ink">{formatDateLong(date)}</h2>
            <ul className="flex flex-col gap-3">
              {meals.map((meal) => (
                <MealCard
                  key={meal.id}
                  meal={meal}
                  householdId={householdId}
                  mealPlanId={mealPlanId}
                  currency={currency}
                  onRegenerateStarted={goToEstado}
                />
              ))}
            </ul>
          </section>
        ))
      )}

      <div className="flex justify-end">
        <Link
          href={`/planes/${mealPlanId}/compra?householdId=${householdId}`}
          className="inline-flex h-11 items-center justify-center rounded-lg bg-primary px-5 text-[0.95rem] font-medium text-primary-ink shadow-sm transition-colors hover:bg-primary-strong"
        >
          Ver lista de la compra
        </Link>
      </div>
    </div>
  );
}
