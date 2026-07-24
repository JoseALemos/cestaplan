"use client";

import { ACTION_CODE_LABELS, actionLabel, readinessStatusLabel } from "@/lib/domain/labels";
import { useReadinessQuery } from "@/lib/query/hooks/use-admin";
import { formatDateTime } from "@/lib/utils/format";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import type { BadgeTone } from "@/components/ui/Badge";

function statusTone(status: string): BadgeTone {
  switch (status) {
    case "available":
      return "success";
    case "ready_for_review":
      return "info";
    case "staging_only":
    case "pending_mappings":
      return "warning";
    default:
      return "neutral";
  }
}

function StatTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border border-border bg-surface p-4">
      <p className="text-xs text-ink-muted">{label}</p>
      <p className="font-display text-display-lg text-ink">{value}</p>
    </div>
  );
}

export default function AdminReadinessPage() {
  const readinessQuery = useReadinessQuery();
  const readiness = readinessQuery.data;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Preparación del planificador</h1>
        <p className="mt-1 text-ink-muted">
          Estado de los datos que necesita el planificador para generar planes: recetas, precios,
          mapeos y cadenas disponibles.
        </p>
      </div>

      {readinessQuery.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : readinessQuery.isError ? (
        <Alert tone="error">No se pudo cargar el estado de preparación del planificador.</Alert>
      ) : readiness ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Estado global</CardTitle>
              <CardDescription>
                Resumen del estado del pipeline de datos del planificador.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="flex items-center gap-3">
                <Badge tone={statusTone(readiness.status)}>
                  {readinessStatusLabel(readiness.status)}
                </Badge>
                <span className="text-sm text-ink-muted">
                  Última sincronización:{" "}
                  {readiness.last_sync_at ? formatDateTime(readiness.last_sync_at) : "Nunca"}
                </span>
              </div>

              {readiness.blockers.length > 0 ? (
                <div className="flex flex-col gap-1.5">
                  <p className="text-sm font-semibold text-ink">Qué falta</p>
                  <ul className="flex flex-col gap-1 text-sm text-ink-muted">
                    {readiness.blockers.map((blocker) => (
                      <li key={blocker}>
                        · {blocker in ACTION_CODE_LABELS ? actionLabel(blocker) : blocker}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </CardContent>
          </Card>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <StatTile label="Recetas activas" value={readiness.recipes_active} />
            <StatTile label="Recetas costeables" value={readiness.recipes_costable} />
            <StatTile label="Ingredientes" value={readiness.ingredients} />
            <StatTile label="Mapeos aprobados" value={readiness.approved_mappings} />
            <StatTile label="Productos staging" value={readiness.staging_products} />
            <StatTile label="Productos productivos" value={readiness.productive_products} />
            <StatTile label="Precios productivos" value={readiness.productive_prices} />
            <StatTile
              label="Cadenas disponibles"
              value={`${readiness.chains_available} / ${readiness.total_chains}`}
            />
            <StatTile label="Observaciones staging" value={readiness.staging_observations} />
          </div>
        </>
      ) : null}
    </div>
  );
}
