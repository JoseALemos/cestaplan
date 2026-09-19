"use client";

import type { MealPlanDetail } from "@/lib/api/types";
import { formatMoney } from "@/lib/utils/format";

export interface SavingsVsHabitualProps {
  plan: MealPlanDetail;
  /** Household's declared usual weekly grocery spend (money string), or null if not set. */
  habitualWeeklySpend: string | null;
}

/** Number of distinct days the plan covers (from cost_per_day, else the date range). */
function planDays(plan: MealPlanDetail): number {
  const perDay = plan.totals?.cost_per_day;
  if (perDay && Object.keys(perDay).length > 0) return Object.keys(perDay).length;
  const start = new Date(plan.start_date);
  const end = new Date(plan.end_date);
  const diff = Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1;
  return diff > 0 ? diff : 1;
}

/**
 * Estimates savings against the household's *declared* usual weekly spend — an honest, CONDITIONAL
 * reference ("if this plan covered your usual shop..."), never an unqualified claim, because a plan
 * may not cover every meal the household eats. Renders nothing unless a habitual spend is set and
 * the plan is actually priced (a 0 € uncosted plan would produce a misleading "you save everything").
 */
export function SavingsVsHabitual({ plan, habitualWeeklySpend }: SavingsVsHabitualProps) {
  const currency = plan.budget.currency;
  const weekly = habitualWeeklySpend != null ? Number(habitualWeeklySpend) : NaN;
  const planCost = plan.totals ? Number(plan.totals.cost_total.total) : NaN;
  if (!Number.isFinite(weekly) || weekly <= 0) return null;
  if (!Number.isFinite(planCost) || planCost <= 0) return null; // uncosted plan: no honest compare

  const days = planDays(plan);
  const habitualForPeriod = (weekly * days) / 7;
  const savings = habitualForPeriod - planCost;
  const saves = savings >= 0;

  return (
    <section className="rounded-lg border border-border bg-bg-subtle p-4">
      <p className="text-sm font-medium text-ink">Frente a tu gasto habitual</p>
      <div className="mt-2 flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm">
        <span className="text-ink-muted">
          Habitual ({days} {days === 1 ? "día" : "días"}):{" "}
          <span className="font-medium text-ink">{formatMoney(habitualForPeriod, currency)}</span>
        </span>
        <span className="text-ink-muted">
          Este plan: <span className="font-medium text-ink">{formatMoney(planCost, currency)}</span>
        </span>
      </div>
      <p className={`mt-2 text-sm font-semibold ${saves ? "text-success" : "text-warning"}`}>
        {saves
          ? `Si cubre tu compra de esos días, ahorrarías ~${formatMoney(savings, currency)}`
          : `Costaría ~${formatMoney(-savings, currency)} más que tu gasto habitual`}
      </p>
      <p className="mt-1 text-xs text-ink-muted">
        Estimación orientativa: compara el coste del plan con tu gasto semanal declarado
        ({formatMoney(weekly, currency)}/sem) prorrateado a {days} {days === 1 ? "día" : "días"}.
      </p>
    </section>
  );
}
