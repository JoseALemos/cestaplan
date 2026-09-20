"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { useQuickStartMutation } from "@/lib/query/hooks/use-plans";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function EmpezarPage() {
  const router = useRouter();
  const { isAuthenticated, isLoading: authLoading } = useAuth();

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/registro");
    }
  }, [authLoading, isAuthenticated, router]);

  const [people, setPeople] = useState(2);
  const [budget, setBudget] = useState("90");
  const quickStart = useQuickStartMutation();

  if (authLoading || !isAuthenticated) {
    return null;
  }

  const onGenerate = () => {
    const parsedBudget = Number(budget.replace(",", "."));
    quickStart.mutate(
      {
        people,
        budget_amount:
          Number.isFinite(parsedBudget) && parsedBudget > 0 ? parsedBudget.toFixed(2) : undefined,
      },
      {
        onSuccess: (data) => {
          const params = new URLSearchParams({
            mealPlanId: data.meal_plan_id,
            householdId: data.household_id,
          });
          router.push(`/planes/estado/${data.optimization_run_id}?${params.toString()}`);
        },
      },
    );
  };

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink text-balance">
          Tu primer plan, en un minuto
        </h1>
        <p className="mt-2 text-ink-muted">
          Generamos un plan real de comidas y cenas para una semana con precios reales de
          supermercado. Podrás ajustarlo todo —personas, presupuesto, alergias, comidas—
          cuando quieras.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Empezamos con lo básico</CardTitle>
          <CardDescription>Dos datos y listo. El resto se afina después.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Input
            label="¿Para cuántas personas?"
            type="number"
            min={1}
            max={50}
            value={String(people)}
            onChange={(event) =>
              setPeople(Math.max(1, Math.min(50, Number(event.target.value) || 1)))
            }
          />
          <Input
            label="Presupuesto semanal aproximado (€)"
            inputMode="decimal"
            value={budget}
            onChange={(event) => setBudget(event.target.value)}
            hint="Solo es un punto de partida; el plan busca el coste más bajo."
          />

          <Alert tone="info">
            Este primer plan aún no conoce tus <b>alergias ni preferencias</b>. Añádelas
            después en los ajustes del hogar para que el plan las tenga en cuenta.
          </Alert>

          {quickStart.isError ? (
            <Alert tone="error">
              No se pudo generar el plan. Inténtalo de nuevo en un momento.
            </Alert>
          ) : null}

          <Button
            type="button"
            size="lg"
            loading={quickStart.isPending}
            onClick={onGenerate}
          >
            Generar mi primer plan
          </Button>

          <Link
            href="/onboarding/hogar"
            className="text-center text-sm font-medium text-ink-muted underline hover:text-ink"
          >
            Prefiero configurarlo paso a paso
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
