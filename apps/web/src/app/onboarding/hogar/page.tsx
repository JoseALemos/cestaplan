"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";

import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { type HouseholdFormValues, householdSchema } from "@/lib/onboarding/schemas";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function HogarPage() {
  const router = useRouter();
  const { state, setHousehold } = useOnboarding();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<HouseholdFormValues>({
    resolver: zodResolver(householdSchema),
    values: state.household ?? { name: "", currency: "EUR" },
  });

  const onSubmit = handleSubmit((values) => {
    setHousehold(values);
    router.push("/onboarding/tienda");
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Tu hogar</CardTitle>
        <CardDescription>
          Dale un nombre a tu hogar. Más adelante podrás invitar a otras personas con permisos
          de editor o solo lectura.
        </CardDescription>
      </CardHeader>
      <form onSubmit={onSubmit} noValidate>
        <CardContent className="flex flex-col gap-4">
          <Input
            label="Nombre del hogar"
            placeholder="p. ej. Casa de Ana y Marcos"
            hint="Solo lo verá tu hogar. Puedes cambiarlo cuando quieras."
            required
            error={errors.name?.message}
            {...register("name")}
          />
          <Input
            label="Moneda"
            placeholder="EUR"
            hint="Código ISO de 3 letras."
            maxLength={3}
            required
            error={errors.currency?.message}
            {...register("currency")}
          />
        </CardContent>
        <div className="mt-2 flex items-center justify-between">
          <Button type="button" variant="ghost" size="sm" disabled>
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
