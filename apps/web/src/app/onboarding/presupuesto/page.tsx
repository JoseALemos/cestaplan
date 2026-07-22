"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import type { z } from "zod";

import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { budgetSchema } from "@/lib/onboarding/schemas";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";

export default function PresupuestoPage() {
  const router = useRouter();
  const { state, setBudget } = useOnboarding();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<z.input<typeof budgetSchema>, unknown, z.output<typeof budgetSchema>>({
    resolver: zodResolver(budgetSchema),
    values: state.budget ?? {
      amount: "",
      currency: state.household?.currency ?? "EUR",
      mode: "strict",
      marginPercent: 10,
    },
  });

  const onSubmit = handleSubmit((values) => {
    setBudget(values);
    router.push("/onboarding/comidas");
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Presupuesto</CardTitle>
        <CardDescription>
          El importe es una restricción real, no una estimación: el plan se ajusta a él.
        </CardDescription>
      </CardHeader>
      <form onSubmit={onSubmit} noValidate>
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
            <Input
              label="Presupuesto objetivo"
              type="text"
              inputMode="decimal"
              placeholder="80.00"
              required
              error={errors.amount?.message}
              {...register("amount")}
            />
            <Input
              label="Moneda"
              maxLength={3}
              required
              error={errors.currency?.message}
              {...register("currency")}
            />
          </div>
          <Select
            label="Modo"
            hint="Estricto: nunca supera el presupuesto. Flexible: permite un margen si hace falta para cubrir el resto de restricciones."
            options={[
              { value: "strict", label: "Estricto" },
              { value: "flexible", label: "Flexible (con margen)" },
            ]}
            {...register("mode")}
          />
          <Input
            label="Margen (%)"
            type="number"
            min={0}
            max={50}
            hint="Solo aplica en modo flexible."
            error={errors.marginPercent?.message}
            {...register("marginPercent")}
          />
          <Alert tone="info">
            El presupuesto y la moneda son lo único que hoy envía la API al generar el plan; el
            modo y el margen ayudan a fijar tus expectativas y se usan si el plan resulta
            inviable con el importe exacto.
          </Alert>
        </CardContent>
        <div className="mt-2 flex items-center justify-between">
          <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/equipamiento")}>
            Atrás
          </Button>
          <Button type="submit" size="sm">
            Continuar
          </Button>
        </div>
      </form>
    </Card>
  );
}
