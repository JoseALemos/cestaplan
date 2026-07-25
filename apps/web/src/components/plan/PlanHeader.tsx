"use client";

import {
  coverageLabel,
  coverageTone,
  isBackendPriceCoverageWarning,
  priceCoverageState,
  PRICE_COVERAGE_NOTICE,
} from "@/lib/domain/labels";
import { formatMoney } from "@/lib/utils/format";
import type { MealPlanDetail } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";

export interface PlanHeaderProps {
  plan: MealPlanDetail;
  onRegenerate: () => void;
  regenerating: boolean;
}

export function PlanHeader({ plan, onRegenerate, regenerating }: PlanHeaderProps) {
  const currency = plan.budget.currency;
  const budgetDiffNumeric = Number(plan.budget_diff);
  const isOverBudget = !Number.isNaN(budgetDiffNumeric) && budgetDiffNumeric < 0;

  // When the plan is uncosted (no prices) the "0,00 €" figures and the full-budget
  // "difference" are misleading, so we show "Sin datos" and a localized notice.
  const coverageState = priceCoverageState(plan.coverage);
  const noPriceData = coverageState === "none";
  const notice = coverageState === "ok" ? null : PRICE_COVERAGE_NOTICE[coverageState];
  // Our notice replaces the engine's raw English price-coverage warning; keep any others verbatim.
  const otherWarnings = plan.warnings.filter((warning) => !isBackendPriceCoverageWarning(warning));

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle>Tu plan de comidas</CardTitle>
            <p className="mt-1 text-sm text-ink-muted">
              {plan.start_date} — {plan.end_date}
            </p>
          </div>
          <Badge tone={coverageTone(plan.coverage?.status)}>{coverageLabel(plan.coverage?.status)}</Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-md border border-border p-3">
            <p className="text-xs text-ink-muted">Presupuesto</p>
            <p className="font-display text-display-sm text-ink">{formatMoney(plan.budget.amount, currency)}</p>
          </div>
          <div className="rounded-md border border-border p-3">
            <p className="text-xs text-ink-muted">Coste conocido</p>
            <p className="font-display text-display-sm text-ink">
              {noPriceData ? "Sin datos" : formatMoney(plan.totals?.cost_total?.known, currency)}
            </p>
          </div>
          <div className="rounded-md border border-border p-3">
            <p className="text-xs text-ink-muted">Diferencia con el presupuesto</p>
            {noPriceData ? (
              <p className="font-display text-display-sm text-ink-muted">Sin datos</p>
            ) : (
              <p className={`font-display text-display-sm ${isOverBudget ? "text-error" : "text-success"}`}>
                {formatMoney(plan.budget_diff, currency)}
              </p>
            )}
          </div>
        </div>

        {notice ? (
          <Alert tone={notice.tone} title={notice.title}>
            {notice.body}
          </Alert>
        ) : null}

        {otherWarnings.length > 0 ? (
          <Alert tone="warning" title="Avisos">
            <ul className="flex flex-col gap-1">
              {otherWarnings.map((warning) => (
                <li key={warning}>· {warning}</li>
              ))}
            </ul>
          </Alert>
        ) : null}

        <div className="flex justify-end">
          <Button type="button" variant="outline" size="sm" loading={regenerating} onClick={onRegenerate}>
            Regenerar todo el plan
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
