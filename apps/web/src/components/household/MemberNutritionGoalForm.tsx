"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import type { z } from "zod";

import type { MemberResponse } from "@/lib/api/types";
import { memberNutritionGoalSchema } from "@/lib/onboarding/schemas";
import { useUpdateMemberMutation } from "@/lib/query/hooks/use-households";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export interface MemberNutritionGoalFormProps {
  householdId: string;
  member: MemberResponse;
}

/**
 * Edits one member's optional nutrition target (kcal/protein per day) after onboarding.
 * Reference-value helper only — the person enters their own numbers, this is never a
 * prescribed diet. Both fields empty clears the target (`nutrition_goal: null`).
 */
export function MemberNutritionGoalForm({ householdId, member }: MemberNutritionGoalFormProps) {
  const updateMember = useUpdateMemberMutation(householdId);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<
    z.input<typeof memberNutritionGoalSchema>,
    unknown,
    z.output<typeof memberNutritionGoalSchema>
  >({
    resolver: zodResolver(memberNutritionGoalSchema),
    values: {
      energy_target_kcal: member.profile?.energy_target_kcal ?? "",
      protein_target_g: member.profile?.protein_target_g ?? "",
    },
  });

  const onSubmit = handleSubmit(async (values) => {
    const hasGoal = values.energy_target_kcal !== "" || values.protein_target_g !== "";
    try {
      await updateMember.mutateAsync({
        memberId: member.id,
        body: {
          nutrition_goal: hasGoal
            ? {
                energy_target_kcal: values.energy_target_kcal === "" ? null : values.energy_target_kcal,
                protein_target_g: values.protein_target_g === "" ? null : values.protein_target_g,
                carb_target_g: null,
                fat_target_g: null,
              }
            : null,
        },
      });
    } catch {
      // surfaced below via updateMember.error
    }
  });

  return (
    <form
      onSubmit={onSubmit}
      noValidate
      className="flex flex-col gap-3 rounded-md border border-border bg-bg-subtle p-3"
    >
      <p className="text-sm font-medium text-ink">Meta nutricional (opcional)</p>
      {updateMember.isError ? (
        <Alert tone="error">No se pudo guardar la meta nutricional. Inténtalo de nuevo.</Alert>
      ) : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <Input
          label="Energía objetivo (kcal/día)"
          type="number"
          min={0}
          max={20000}
          placeholder="2000"
          error={errors.energy_target_kcal?.message}
          {...register("energy_target_kcal")}
        />
        <Input
          label="Proteína objetivo (g/día)"
          type="number"
          min={0}
          max={2000}
          placeholder="50"
          error={errors.protein_target_g?.message}
          {...register("protein_target_g")}
        />
      </div>
      <p className="text-xs text-ink-muted">
        Orientativo. La referencia europea de ingesta es ~2.000 kcal/día para un adulto;
        ajústalo a tu caso. No es consejo médico.
      </p>
      <Button
        type="submit"
        size="sm"
        variant="secondary"
        loading={updateMember.isPending}
        className="self-start"
      >
        Guardar meta nutricional
      </Button>
      {updateMember.isSuccess ? <p className="text-xs text-success">Guardado ✓</p> : null}
    </form>
  );
}
