"use client";

import Link from "next/link";

import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { planStatusLabel, planStatusTone } from "@/lib/domain/labels";
import { useHouseholdQuery } from "@/lib/query/hooks/use-households";
import { usePlansQuery } from "@/lib/query/hooks/use-plans";
import { formatDate, formatMoney } from "@/lib/utils/format";
import type { PlanSummary } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";

/** Total plan cost (known + estimated), or null when the plan has no costed grocery list yet. */
function planCost(plan: PlanSummary): number | null {
  if (plan.cost_known == null && plan.cost_estimated == null) return null;
  return Number(plan.cost_known ?? 0) + Number(plan.cost_estimated ?? 0);
}

function planDays(plan: PlanSummary): number {
  const d = Math.round(
    (new Date(plan.end_date).getTime() - new Date(plan.start_date).getTime()) / 86_400_000,
  ) + 1;
  return d > 0 ? d : 1;
}

/** Estimated savings of one plan vs the household's declared weekly spend, prorated to its days. */
function planSavings(plan: PlanSummary, weekly: number): number | null {
  const cost = planCost(plan);
  if (cost == null) return null;
  return (weekly * planDays(plan)) / 7 - cost;
}

function SavingsSummary({ plans, weekly, currency }: { plans: PlanSummary[]; weekly: number; currency: string }) {
  const costed = plans.filter((p) => planCost(p) != null);
  if (costed.length === 0) return null;
  const total = costed.reduce((acc, p) => acc + (planSavings(p, weekly) ?? 0), 0);
  const positive = total >= 0;
  return (
    <div className="rounded-lg border border-border bg-bg-subtle p-4">
      <p className="text-sm text-ink-muted">Frente a tu gasto habitual, en {costed.length}{" "}
        {costed.length === 1 ? "plan" : "planes"}</p>
      <p className={`mt-1 font-display text-display-md ${positive ? "text-success" : "text-warning"}`}>
        {positive ? "Has ahorrado ~" : "Has gastado ~"}
        {formatMoney(Math.abs(total), currency)}
      </p>
      <p className="mt-1 text-xs text-ink-muted">
        Estimación: asume que cada plan cubrió tu compra de esos días
        ({formatMoney(weekly, currency)}/sem declarado).
      </p>
    </div>
  );
}

function PlanRow({
  plan,
  householdId,
  weekly,
}: {
  plan: PlanSummary;
  householdId: string;
  weekly: number | null;
}) {
  const cost = planCost(plan);
  const savings = weekly != null ? planSavings(plan, weekly) : null;
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
          <p className="text-xs text-ink-muted">
            {plan.meal_count} {plan.meal_count === 1 ? "comida" : "comidas"}
            {plan.retailer_name ? ` · ${plan.retailer_name}` : ""} · creado el{" "}
            {formatDate(plan.created_at)}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {savings != null && savings > 0 ? (
            <span className="text-xs font-medium text-success">
              ahorras ~{formatMoney(savings, plan.currency)}
            </span>
          ) : null}
          <span className="text-sm font-medium text-ink">
            {cost != null ? formatMoney(cost, plan.currency) : formatMoney(plan.budget_amount, plan.currency)}
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
  const householdQuery = useHouseholdQuery(householdId);

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

  const plans = plansQuery.data ?? [];
  const weeklyRaw = householdQuery.data?.habitual_weekly_spend;
  const weekly = weeklyRaw != null && Number(weeklyRaw) > 0 ? Number(weeklyRaw) : null;
  const currency = householdQuery.data?.currency ?? plans[0]?.currency ?? "EUR";

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Mis planes</h1>
        <p className="mt-2 text-ink-muted">Historial de planes de comida generados para este hogar.</p>
      </div>

      {weekly != null ? (
        <SavingsSummary plans={plans} weekly={weekly} currency={currency} />
      ) : plans.length > 0 ? (
        <Alert tone="info">
          Añade tu <b>gasto semanal habitual</b> en{" "}
          <Link href={`/households/${householdId}/ajustes`} className="font-medium underline">
            los ajustes del hogar
          </Link>{" "}
          y verás cuánto ahorras con cada plan.
        </Alert>
      ) : null}

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
          ) : plans.length === 0 ? (
            <Alert tone="info" title="Aún no has generado ningún plan">
              Genera tu primer plan de comidas para este hogar.{" "}
              <Link href={`/households/${householdId}/generar`} className="font-medium underline">
                Generar plan
              </Link>
            </Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {plans.map((plan) => (
                <PlanRow key={plan.id} plan={plan} householdId={householdId} weekly={weekly} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
