"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import type { z } from "zod";

import { ApiError } from "@/lib/api/client";
import type { HouseholdResponse } from "@/lib/api/types";
import { householdSpendSchema } from "@/lib/onboarding/schemas";
import { useUpdateHouseholdMutation } from "@/lib/query/hooks/use-households";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export interface HabitualSpendFormProps {
  householdId: string;
  household: HouseholdResponse;
}

/**
 * Edits the household's declared usual weekly grocery spend — a reference the person enters
 * themselves so a plan can estimate "you'd usually spend X, this plan is Y". Empty clears it.
 */
export function HabitualSpendForm({ householdId, household }: HabitualSpendFormProps) {
  const updateHousehold = useUpdateHouseholdMutation(householdId);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<
    z.input<typeof householdSpendSchema>,
    unknown,
    z.output<typeof householdSpendSchema>
  >({
    resolver: zodResolver(householdSpendSchema),
    values: { habitual_weekly_spend: household.habitual_weekly_spend ?? "" },
  });

  const onSubmit = handleSubmit(async (values) => {
    const spend = values.habitual_weekly_spend;
    try {
      // The PATCH sets the whole basic-settings object, so name + currency ride along unchanged.
      await updateHousehold.mutateAsync({
        name: household.name,
        currency: household.currency,
        habitual_weekly_spend: spend === "" || spend === undefined ? null : String(spend),
      });
    } catch {
      // surfaced below via updateHousehold.error
    }
  });

  const isServerError =
    updateHousehold.isError &&
    !(updateHousehold.error instanceof ApiError && updateHousehold.error.status === 422);

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      {isServerError ? (
        <Alert tone="error">No se pudo guardar el gasto habitual. Inténtalo de nuevo.</Alert>
      ) : null}
      <Input
        label={`Gasto semanal habitual (${household.currency})`}
        type="number"
        min={0}
        max={100000}
        step="0.01"
        placeholder="p. ej. 90"
        hint="Lo que sueles gastar a la semana en la compra. Lo usamos solo para estimar tu ahorro."
        error={errors.habitual_weekly_spend?.message}
        {...register("habitual_weekly_spend")}
      />
      <Button
        type="submit"
        size="sm"
        loading={updateHousehold.isPending}
        className="self-start"
      >
        Guardar gasto habitual
      </Button>
      {updateHousehold.isSuccess ? (
        <p className="text-xs text-success">Guardado ✓</p>
      ) : null}
    </form>
  );
}
