"use client";

import { useState } from "react";

import { ApiError } from "@/lib/api/client";
import { promotionGateReasonLabel } from "@/lib/domain/labels";
import {
  useProviderPromotionActions,
  useProviderPromotionStatusQuery,
} from "@/lib/query/hooks/use-provider-promotion";
import type { ProviderPromotionResult } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Skeleton } from "@/components/ui/Skeleton";

/** A 409 rejection carries `detail.reasons` (typed slugs); translate them, never show raw JSON. */
function gateReasonsFromError(error: unknown): string[] | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = (error.body as { detail?: { reasons?: unknown } } | null)?.detail;
  if (!detail || !Array.isArray(detail.reasons)) return null;
  return detail.reasons.map((reason) => promotionGateReasonLabel(String(reason)));
}

function PromotionCounts({ result }: { result: ProviderPromotionResult }) {
  return (
    <div className="flex flex-wrap gap-4 text-ink-muted">
      <span>Candidatos aprobados: {result.approved_candidates}</span>
      <span>Mapeos creados: {result.mappings_created}</span>
      <span>Observaciones promovidas: {result.observations_promoted}</span>
      <span>Precios escritos: {result.prices_written}</span>
      {result.retailer_ids.length > 0 ? (
        <span>Cadenas afectadas: {result.retailer_ids.join(", ")}</span>
      ) : null}
    </div>
  );
}

export default function PromocionPage() {
  const [provider, setProvider] = useState("");

  const status = useProviderPromotionStatusQuery(provider);
  const actions = useProviderPromotionActions(provider);

  const busy =
    actions.approve.isPending || actions.previewPromotion.isPending || actions.promote.isPending;

  const approveReasons = gateReasonsFromError(actions.approve.error);
  const promoteReasons = gateReasonsFromError(actions.promote.error);

  const onPromote = () => {
    const confirmed = window.confirm(
      `¿Promover "${provider}" a producción? Esta acción escribe mapeos y precios reales.`,
    );
    if (confirmed) actions.promote.mutate();
  };

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="font-display text-display-lg text-ink">Promoción a producción</h1>
        <p className="mt-1 text-ink-muted">
          Comprueba si un proveedor cumple los requisitos para pasar de staging a producción,
          apruébalo y promueve sus mapeos y precios.
        </p>
      </div>

      <Input
        label="Código de proveedor"
        placeholder="parsebot-alcampo"
        value={provider}
        onChange={(e) => setProvider(e.target.value.trim())}
      />

      {!provider ? (
        <Alert tone="info">Introduce un código de proveedor para ver su estado de promoción.</Alert>
      ) : status.isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : status.isError ? (
        <Alert tone="error">No se pudo cargar el estado de promoción de «{provider}».</Alert>
      ) : status.data ? (
        <Card>
          <CardHeader>
            <CardTitle>Estado · {status.data.provider_code}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm">
            <div>
              <Badge tone={status.data.production_ready ? "success" : "warning"}>
                {status.data.production_ready ? "Listo para producción" : "No listo para producción"}
              </Badge>
            </div>
            <div className="flex flex-wrap gap-4 text-ink-muted">
              <span>Candidatos aprobados: {status.data.approved_candidates}</span>
              <span>Observaciones en staging: {status.data.staged_observations}</span>
            </div>
            {status.data.gate_reasons.length > 0 ? (
              <ul className="list-inside list-disc text-ink-muted">
                {status.data.gate_reasons.map((reason) => (
                  <li key={reason}>{promotionGateReasonLabel(reason)}</li>
                ))}
              </ul>
            ) : null}

            <div className="flex flex-wrap gap-2 pt-1">
              <Button
                size="sm"
                variant="primary"
                disabled={busy}
                loading={actions.approve.isPending}
                onClick={() => actions.approve.mutate()}
              >
                Aprobar para producción
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                loading={actions.previewPromotion.isPending}
                onClick={() => actions.previewPromotion.mutate()}
              >
                Previsualizar promoción
              </Button>
              <Button size="sm" variant="danger" disabled={busy} loading={actions.promote.isPending} onClick={onPromote}>
                Promover
              </Button>
            </div>

            {approveReasons ? (
              <Alert tone="error">No se pudo aprobar: {approveReasons.join("; ")}</Alert>
            ) : actions.approve.isError ? (
              <Alert tone="error">No se pudo aprobar el proveedor para producción.</Alert>
            ) : actions.approve.isSuccess ? (
              <Alert tone="success">Proveedor aprobado para producción.</Alert>
            ) : null}

            {promoteReasons ? (
              <Alert tone="error">No se pudo promover: {promoteReasons.join("; ")}</Alert>
            ) : actions.promote.isError ? (
              <Alert tone="error">No se pudo promover el proveedor a producción.</Alert>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {actions.previewPromotion.data ? (
        <Card>
          <CardHeader>
            <CardTitle>Vista previa de la promoción</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            <p className="text-ink-muted">Nada se ha escrito todavía; esto es solo una simulación.</p>
            <PromotionCounts result={actions.previewPromotion.data} />
          </CardContent>
        </Card>
      ) : null}

      {actions.promote.data ? (
        <Card>
          <CardHeader>
            <CardTitle>Promoción aplicada</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            <PromotionCounts result={actions.promote.data} />
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
