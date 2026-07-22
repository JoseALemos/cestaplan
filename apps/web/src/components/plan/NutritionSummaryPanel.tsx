"use client";

import type { MacroStatus, MacroSummary, NutritionSummary } from "@/lib/api/types";

import { Badge, type BadgeTone } from "@/components/ui/Badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";

type MacroKey = "kcal" | "protein_g" | "carbs_g" | "fat_g";

const MACROS: { key: MacroKey; label: string; unit: string }[] = [
  { key: "kcal", label: "Energía", unit: "kcal" },
  { key: "protein_g", label: "Proteína", unit: "g" },
  { key: "carbs_g", label: "Carbohidratos", unit: "g" },
  { key: "fat_g", label: "Grasa", unit: "g" },
];

const STATUS_META: Record<MacroStatus, { tone: BadgeTone; label: string }> = {
  met: { tone: "success", label: "En objetivo" },
  under: { tone: "warning", label: "Por debajo" },
  over: { tone: "accent", label: "Por encima" },
  unknown: { tone: "neutral", label: "Sin objetivo" },
};

function round(value: string | null): string {
  if (value === null) return "—";
  const num = Number(value);
  return Number.isNaN(num) ? "—" : Math.round(num).toString();
}

export interface NutritionSummaryPanelProps {
  summary: NutritionSummary;
}

export function NutritionSummaryPanel({ summary }: NutritionSummaryPanelProps) {
  const rows = MACROS.map((macro) => ({ ...macro, data: summary[macro.key] as MacroSummary })).filter(
    (row) => row.data.target_per_day !== null,
  );

  if (rows.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Objetivos nutricionales</CardTitle>
        <p className="mt-1 text-sm text-ink-muted">Media por día frente a los objetivos del hogar.</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {rows.map(({ key, label, unit, data }) => {
          const status = STATUS_META[data.status] ?? STATUS_META.unknown;
          return (
            <div key={key} className="flex items-center justify-between gap-3 rounded-md border border-border p-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-ink">{label}</p>
                <p className="text-sm text-ink-muted">
                  {round(data.actual_per_day)} {unit}/día · objetivo {round(data.target_per_day)} {unit}
                </p>
              </div>
              <Badge tone={status.tone}>{status.label}</Badge>
            </div>
          );
        })}
        {!summary.complete ? (
          <p className="text-xs text-ink-muted">
            Algunas comidas no tienen datos nutricionales completos; las cifras son aproximadas.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
