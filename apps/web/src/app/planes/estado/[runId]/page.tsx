"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

import { RUN_STATUS_LABELS, RUN_STATUS_ORDER } from "@/lib/domain/labels";
import { infeasibilityView } from "@/lib/domain/plan-infeasibility";
import { useRegeneratePlanMutation, useRunStatusQuery } from "@/lib/query/hooks/use-plans";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Skeleton } from "@/components/ui/Skeleton";

export default function EstadoGeneracionPage() {
  const params = useParams<{ runId: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const runId = params.runId;
  const mealPlanIdFromUrl = searchParams.get("mealPlanId");
  const householdId = searchParams.get("householdId");

  const runStatusQuery = useRunStatusQuery(runId);
  const status = runStatusQuery.data?.status;
  const mealPlanId = runStatusQuery.data?.meal_plan_id ?? mealPlanIdFromUrl ?? undefined;

  const regenerateMutation = useRegeneratePlanMutation(mealPlanId ?? "");

  useEffect(() => {
    if (status === "completed" && mealPlanId) {
      router.replace(`/planes/${mealPlanId}`);
    }
  }, [status, mealPlanId, router]);

  const stepIndex = status ? RUN_STATUS_ORDER.indexOf(status) : -1;
  const progressPercent =
    stepIndex >= 0 ? Math.round(((stepIndex + 1) / RUN_STATUS_ORDER.length) * 100) : 0;

  const infeasibility = runStatusQuery.data?.infeasibility;
  const view = infeasibilityView(infeasibility);

  return (
    <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
      <Card>
        <CardHeader>
          <CardTitle>Generando tu plan</CardTitle>
          <CardDescription>
            Esto tarda normalmente entre unos segundos y un par de minutos. Puedes dejar esta
            pantalla abierta.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          {runStatusQuery.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : runStatusQuery.isError ? (
            <Alert tone="error">
              No se pudo consultar el estado de la generación. Comprueba tu conexión.
            </Alert>
          ) : status === "failed" ? (
            <>
              <Alert tone="error" title="No se pudo generar un plan viable">
                {view.message}
              </Alert>

              {view.minimumBudget ? (
                <div className="flex items-center justify-between rounded-md border border-border px-4 py-3 text-sm">
                  <span className="text-ink-muted">Presupuesto mínimo estimado</span>
                  <Badge tone="warning">{view.minimumBudget}</Badge>
                </div>
              ) : null}

              {infeasibility?.offending_products && infeasibility.offending_products.length > 0 ? (
                <div className="flex flex-col gap-1.5">
                  <p className="text-sm font-semibold text-ink">Productos problemáticos</p>
                  <ul className="flex flex-col gap-1 text-sm text-ink-muted">
                    {infeasibility.offending_products.map((product) => (
                      <li key={product.name}>
                        {product.name}
                        {product.reason ? ` — ${product.reason}` : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {view.actions.length > 0 ? (
                <div className="flex flex-col gap-2">
                  <p className="text-sm font-semibold text-ink">Qué puedes hacer</p>
                  <ul className="flex flex-col gap-1 text-sm text-ink-muted">
                    {view.actions.map((action) => (
                      <li key={action.code}>· {action.label}</li>
                    ))}
                  </ul>
                </div>
              ) : null}

              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    loading={regenerateMutation.isPending}
                    onClick={() => mealPlanId && regenerateMutation.mutate()}
                    disabled={!mealPlanId || !view.canRetry}
                  >
                    Reintentar generación
                  </Button>
                  {view.showBudgetAdjust && householdId ? (
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => router.push(`/households/${householdId}/generar`)}
                    >
                      Ajustar presupuesto y volver a generar
                    </Button>
                  ) : null}
                  <Button type="button" variant="ghost" onClick={() => router.push("/households")}>
                    Volver a mis hogares
                  </Button>
                </div>
                {!view.canRetry && view.retryHint ? (
                  <p className="text-sm text-ink-muted">{view.retryHint}</p>
                ) : null}
              </div>
            </>
          ) : status === "cancelled" ? (
            <Alert tone="warning">La generación se canceló.</Alert>
          ) : (
            <>
              <ProgressBar
                value={progressPercent}
                label={status ? RUN_STATUS_LABELS[status] : "Preparando…"}
              />
              <ol className="flex flex-col gap-2">
                {RUN_STATUS_ORDER.slice(0, -1).map((candidate, index) => (
                  <li
                    key={candidate}
                    className="flex items-center gap-3 text-sm"
                    aria-current={candidate === status ? "step" : undefined}
                  >
                    <span
                      aria-hidden="true"
                      className={
                        index <= stepIndex
                          ? "h-2.5 w-2.5 rounded-full bg-accent"
                          : "h-2.5 w-2.5 rounded-full bg-bg-subtle"
                      }
                    />
                    <span className={index <= stepIndex ? "text-ink" : "text-ink-faint"}>
                      {RUN_STATUS_LABELS[candidate]}
                    </span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
