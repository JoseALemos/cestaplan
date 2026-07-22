"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useParams, useRouter } from "next/navigation";
import { useFieldArray, useForm } from "react-hook-form";
import { z } from "zod";

import { ApiError } from "@/lib/api/client";
import { MEAL_TYPE_LABELS, MEAL_TYPE_ORDER } from "@/lib/domain/labels";
import { useHouseholdQuery, useMembersQuery } from "@/lib/query/hooks/use-households";
import { useGeneratePlanMutation } from "@/lib/query/hooks/use-plans";
import { budgetSchema, mealRequirementFormSchema } from "@/lib/onboarding/schemas";
import { addDaysIso, todayIso } from "@/lib/utils/format";
import type { MealRequirementIn } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Skeleton } from "@/components/ui/Skeleton";

const formSchema = z.object({
  budget: budgetSchema.pick({ amount: true, currency: true }),
  meals: z.array(mealRequirementFormSchema),
});

export default function GenerarPlanPage() {
  const params = useParams<{ householdId: string }>();
  const router = useRouter();
  const householdId = params.householdId;

  const householdQuery = useHouseholdQuery(householdId);
  const membersQuery = useMembersQuery(householdId);
  const generateMutation = useGeneratePlanMutation();

  const eatingMembers = Math.max(
    (membersQuery.data ?? []).filter((member) => member.is_eater).length,
    1,
  );

  const {
    control,
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<z.input<typeof formSchema>, unknown, z.output<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    values: householdQuery.data
      ? {
          budget: { amount: "", currency: householdQuery.data.currency },
          meals: MEAL_TYPE_ORDER.map((mealType) => ({
            meal_type: mealType,
            requested_count: 0,
            default_servings: eatingMembers,
            maximum_preparation_minutes: "" as const,
            requires_tupper: false,
          })),
        }
      : undefined,
  });

  const { fields } = useFieldArray({ control, name: "meals" });

  const onSubmit = handleSubmit(async (values) => {
    const requirements: MealRequirementIn[] = values.meals
      .filter((meal) => meal.requested_count > 0)
      .map((meal) => ({
        meal_type: meal.meal_type,
        requested_count: meal.requested_count,
        default_servings: meal.default_servings,
        maximum_preparation_minutes:
          meal.maximum_preparation_minutes === "" || meal.maximum_preparation_minutes === undefined
            ? null
            : Number(meal.maximum_preparation_minutes),
        requires_tupper: meal.requires_tupper,
      }));

    if (requirements.length === 0) return;

    const start = todayIso();
    const accepted = await generateMutation.mutateAsync({
      household_id: householdId,
      start_date: start,
      end_date: addDaysIso(start, 6),
      budget_amount: values.budget.amount,
      currency: values.budget.currency,
      requirements,
    });

    router.push(
      `/planes/estado/${accepted.optimization_run_id}?mealPlanId=${accepted.meal_plan_id}&householdId=${householdId}`,
    );
  });

  if (householdQuery.isLoading || membersQuery.isLoading) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (householdQuery.isError || !householdQuery.data) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
        <Alert tone="error">No se pudo cargar este hogar.</Alert>
      </div>
    );
  }

  const errorMessage =
    generateMutation.error instanceof ApiError
      ? generateMutation.error.status === 422
        ? "Revisa el presupuesto y las comidas solicitadas."
        : "No se pudo generar el plan. Inténtalo de nuevo."
      : null;

  return (
    <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
      <Card>
        <CardHeader>
          <CardTitle>Generar plan para {householdQuery.data.name}</CardTitle>
          <CardDescription>
            Próximos 7 días, a partir de hoy. Ajusta presupuesto y comidas para esta tanda.
          </CardDescription>
        </CardHeader>
        <form onSubmit={onSubmit} noValidate>
          <CardContent className="flex flex-col gap-5">
            {errorMessage ? <Alert tone="error">{errorMessage}</Alert> : null}
            <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
              <Input
                label="Presupuesto objetivo"
                inputMode="decimal"
                placeholder="80.00"
                required
                error={errors.budget?.amount?.message}
                {...register("budget.amount")}
              />
              <Input
                label="Moneda"
                maxLength={3}
                required
                error={errors.budget?.currency?.message}
                {...register("budget.currency")}
              />
            </div>

            {fields.map((field, index) => (
              <div key={field.id} className="flex flex-col gap-3 rounded-md border border-border p-4">
                <p className="font-display text-display-sm text-ink">
                  {MEAL_TYPE_LABELS[field.meal_type]}
                </p>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Input
                    label="Nº de comidas"
                    type="number"
                    min={0}
                    max={100}
                    error={errors.meals?.[index]?.requested_count?.message}
                    {...register(`meals.${index}.requested_count`)}
                  />
                  <Input
                    label="Raciones"
                    type="number"
                    min={1}
                    max={50}
                    error={errors.meals?.[index]?.default_servings?.message}
                    {...register(`meals.${index}.default_servings`)}
                  />
                  <Input
                    label="Prep. máx. (min)"
                    type="number"
                    min={0}
                    max={1440}
                    placeholder="sin límite"
                    {...register(`meals.${index}.maximum_preparation_minutes`)}
                  />
                </div>
                <label className="flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-primary"
                    {...register(`meals.${index}.requires_tupper`)}
                  />
                  Apto para tupper / llevar
                </label>
              </div>
            ))}
          </CardContent>
          <div className="mt-2 flex items-center justify-between">
            <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/households")}>
              Volver
            </Button>
            <Button type="submit" size="sm" loading={generateMutation.isPending}>
              Generar plan
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
