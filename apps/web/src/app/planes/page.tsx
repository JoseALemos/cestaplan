"use client";

import Link from "next/link";

import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { planStatusLabel, planStatusTone } from "@/lib/domain/labels";
import { usePlansQuery } from "@/lib/query/hooks/use-plans";
import { formatDate, formatMoney } from "@/lib/utils/format";
import type { PlanSummary } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";

function PlanRow({ plan, householdId }: { plan: PlanSummary; householdId: string }) {
  return (
    <li>
      <Link
        href={`/planes/${plan.id}?householdId=${householdId}`}
        className="flex flex-col gap-2 rounded-md border border-border px-4 py-3 transition-colors hover:border-primary hover:bg-bg-subtle sm:flex-row sm:items-center sm:justify-between"
      >
        <div>
          <p className="text-sm font-medium text-ink">
            {formatDate(plan.start_date)} – {formatDate(plan.end_date)}
          </p>
          <p className="text-xs text-ink-faint">
            {plan.meal_count} {plan.meal_count === 1 ? "comida" : "comidas"}
            {plan.retailer_name ? ` · ${plan.retailer_name}` : ""} · creado el{" "}
            {formatDate(plan.created_at)}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-ink">
            {formatMoney(plan.budget_amount, plan.currency)}
          </span>
          <Badge tone={planStatusTone(plan.status)}>{planStatusLabel(plan.status)}</Badge>
        </div>
      </Link>
    </li>
  );
}

export default function PlanesPage() {
  const [householdId] = useCurrentHouseholdId();
  const plansQuery = usePlansQuery(householdId);

  if (!householdId) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <Alert tone="info" title="Selecciona un hogar">
          Elige un hogar para ver su historial de planes.{" "}
          <Link href="/households" className="font-medium underline">
            Ir a hogares
          </Link>
        </Alert>
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Mis planes</h1>
        <p className="mt-2 text-ink-muted">Historial de planes de comida generados para este hogar.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Planes generados</CardTitle>
          <CardDescription>De más reciente a más antiguo.</CardDescription>
        </CardHeader>
        <CardContent>
          {plansQuery.isLoading ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : plansQuery.isError ? (
            <Alert tone="error">No se pudieron cargar tus planes. Comprueba tu conexión.</Alert>
          ) : (plansQuery.data ?? []).length === 0 ? (
            <Alert tone="info" title="Aún no has generado ningún plan">
              Genera tu primer plan de comidas para este hogar.{" "}
              <Link href={`/households/${householdId}/generar`} className="font-medium underline">
                Generar plan
              </Link>
            </Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {(plansQuery.data ?? []).map((plan) => (
                <PlanRow key={plan.id} plan={plan} householdId={householdId} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
