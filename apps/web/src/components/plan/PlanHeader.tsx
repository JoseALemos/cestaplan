"use client";

import { coverageLabel, coverageTone } from "@/lib/domain/labels";
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
              {formatMoney(plan.totals?.cost_total?.known, currency)}
            </p>
          </div>
          <div className="rounded-md border border-border p-3">
            <p className="text-xs text-ink-muted">Diferencia con el presupuesto</p>
            <p className={`font-display text-display-sm ${isOverBudget ? "text-error" : "text-success"}`}>
              {formatMoney(plan.budget_diff, currency)}
            </p>
          </div>
        </div>

        {plan.warnings.length > 0 ? (
          <Alert tone="warning" title="Avisos">
            <ul className="flex flex-col gap-1">
              {plan.warnings.map((warning) => (
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
