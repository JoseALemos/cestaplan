"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useFieldArray, useForm } from "react-hook-form";
import { z } from "zod";

import { MEAL_TYPE_LABELS, MEAL_TYPE_ORDER } from "@/lib/domain/labels";
import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { mealRequirementFormSchema } from "@/lib/onboarding/schemas";
import type { MealRequirementIn } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

const formSchema = z.object({ meals: z.array(mealRequirementFormSchema) });

export default function ComidasPage() {
  const router = useRouter();
  const { state, setMealRequirements } = useOnboarding();

  const eatingMembers = Math.max(
    state.members.filter((member) => member.is_eater).length,
    1,
  );

  const defaultMeals = MEAL_TYPE_ORDER.map((mealType) => {
    const existing = state.mealRequirements.find((requirement) => requirement.meal_type === mealType);
    return {
      meal_type: mealType,
      requested_count: existing?.requested_count ?? 0,
      default_servings: existing?.default_servings ?? eatingMembers,
      maximum_preparation_minutes:
        existing?.maximum_preparation_minutes != null
          ? existing.maximum_preparation_minutes
          : ("" as const),
      requires_tupper: existing?.requires_tupper ?? false,
    };
  });

  const {
    control,
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<z.input<typeof formSchema>, unknown, z.output<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: { meals: defaultMeals },
  });

  const { fields } = useFieldArray({ control, name: "meals" });

  const onSubmit = handleSubmit((values) => {
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
    setMealRequirements(requirements);
    router.push("/onboarding/resumen");
  });

  const hasAnyMeal = fields.length > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Comidas requeridas</CardTitle>
        <CardDescription>
          No hace falta llenar todos los huecos: deja en 0 lo que no necesites. Puedes pedir
          tupper para llevar o limitar el tiempo de preparación.
        </CardDescription>
      </CardHeader>
      <form onSubmit={onSubmit} noValidate>
        <CardContent className="flex flex-col gap-5">
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
                  label="Raciones por comida"
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
                  error={errors.meals?.[index]?.maximum_preparation_minutes?.message}
                  {...register(`meals.${index}.maximum_preparation_minutes`)}
                />
              </div>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-primary"
                  {...register(`meals.${index}.requires_tupper`)}
                />
                Necesito que sea apto para tupper / llevar
              </label>
            </div>
          ))}
          {!hasAnyMeal ? <Alert tone="error">No se pudieron cargar los tipos de comida.</Alert> : null}
        </CardContent>
        <div className="mt-2 flex items-center justify-between">
          <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/presupuesto")}>
            Atrás
          </Button>
          <Button type="submit" size="sm">
            Continuar al resumen
          </Button>
        </div>
      </form>
    </Card>
  );
}
