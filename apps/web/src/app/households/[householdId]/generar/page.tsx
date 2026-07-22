"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { useFieldArray, useForm } from "react-hook-form";
import { z } from "zod";

import { ApiError } from "@/lib/api/client";
import { MEAL_TYPE_LABELS, MEAL_TYPE_ORDER } from "@/lib/domain/labels";
import { useHouseholdQuery, useMembersQuery } from "@/lib/query/hooks/use-households";
import { useRetailersQuery, useStoresQuery } from "@/lib/query/hooks/use-catalog";
import { useGeneratePlanMutation } from "@/lib/query/hooks/use-plans";
import { budgetSchema, mealRequirementFormSchema } from "@/lib/onboarding/schemas";
import { addDaysIso, todayIso } from "@/lib/utils/format";
import type { MealRequirementIn } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";

const formSchema = z.object({
  budget: budgetSchema.pick({ amount: true, currency: true, priority: true }),
  meals: z.array(mealRequirementFormSchema),
});

export default function GenerarPlanPage() {
  const params = useParams<{ householdId: string }>();
  const router = useRouter();
  const householdId = params.householdId;

  const householdQuery = useHouseholdQuery(householdId);
  const membersQuery = useMembersQuery(householdId);
  const generateMutation = useGeneratePlanMutation();

  // Chain (retailer) selection. Optional; omitted lets the backend use the household's
  // default chain. Prices are aggregated across ALL of the chain's stores — "la tienda da
  // igual" — so there is no specific-store sub-selection.
  const retailersQuery = useRetailersQuery();
  const [retailerId, setRetailerId] = useState<string>("");
  const selectedRetailer = useMemo(
    () => retailersQuery.data?.find((retailer) => retailer.id === retailerId),
    [retailersQuery.data, retailerId],
  );
  const retailerOptions = (retailersQuery.data ?? []).map((retailer) => ({
    value: retailer.id,
    label: retailer.is_synthetic ? `${retailer.name} (datos sintéticos)` : retailer.name,
  }));
  // Read-only context only: how many of the chain's stores contribute prices.
  const storesQuery = useStoresQuery(retailerId || undefined);
  const storeCount = storesQuery.data?.length ?? null;

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
          budget: { amount: "", currency: householdQuery.data.currency, priority: "waste" },
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
        // Empty, undefined, or 0/non-positive all mean "sin límite" (null).
        maximum_preparation_minutes:
          meal.maximum_preparation_minutes === "" ||
          meal.maximum_preparation_minutes === undefined ||
          Number(meal.maximum_preparation_minutes) <= 0
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
      priority: values.budget.priority,
      retailer_id: retailerId || undefined,
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

            <div className="flex flex-col gap-3 rounded-md border border-border p-4">
              <p className="font-display text-display-sm text-ink">Cadena</p>
              <p className="text-sm text-ink-muted">
                Elige la cadena; usaremos sus precios (de todas sus tiendas).
              </p>
              {retailersQuery.isLoading ? (
                <Skeleton className="h-11 w-full" />
              ) : retailersQuery.isError ? (
                <Alert tone="warning">
                  El catálogo de cadenas no está disponible ahora mismo. Se usará la cadena
                  por defecto de tu hogar.
                </Alert>
              ) : retailerOptions.length === 0 ? (
                <Alert tone="info">Todavía no hay cadenas dadas de alta.</Alert>
              ) : (
                <Select
                  label="Cadena"
                  placeholder="Cadena por defecto del hogar"
                  options={retailerOptions}
                  value={retailerId}
                  onChange={(event) => setRetailerId(event.target.value)}
                />
              )}

              {selectedRetailer ? (
                <p className="text-sm text-ink-muted">
                  {storesQuery.isLoading
                    ? "Cargando cobertura de la cadena…"
                    : storeCount && storeCount > 0
                      ? `Tomaremos los precios más recientes de sus ${storeCount} ${
                          storeCount === 1 ? "tienda" : "tiendas"
                        } con datos. La tienda concreta da igual.`
                      : "Usaremos los precios más recientes de la cadena. La tienda concreta da igual."}
                </p>
              ) : null}
            </div>

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

            <Select
              label="¿Qué priorizamos?"
              hint="Más variedad: aprovecha el presupuesto para maximizar variedad y aprovechamiento. Menor precio: busca el plan más barato posible."
              options={[
                { value: "waste", label: "Más variedad (aprovecha el presupuesto)" },
                { value: "price", label: "Menor precio (lo más barato)" },
              ]}
              {...register("budget.priority")}
            />

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
