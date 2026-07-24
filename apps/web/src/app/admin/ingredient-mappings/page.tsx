"use client";

import { useMemo, useState } from "react";

import type { MappingCandidate, MappingCandidateFilters } from "@/lib/api/types";
import {
  useMappingActions,
  useMappingCandidatesQuery,
  useMappingSummaryQuery,
} from "@/lib/query/hooks/use-mappings";

import { Alert } from "@/components/ui/Alert";
import { Badge, type BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";

const PROVIDERS = ["parsebot-alcampo", "parsebot-carrefour", "parsebot-dia"];
const REVIEW_NOTICE = "Los datos externos están en revisión y no se utilizan en producción.";

// lifecycle_status / relation_status -> label + badge tone (never colour alone: label carries it).
const LIFECYCLE: Record<string, { label: string; tone: BadgeTone }> = {
  approved: { label: "Aprobado", tone: "success" },
  revoked: { label: "Revocado", tone: "warning" },
  rejected: { label: "Rechazado", tone: "error" },
  rejected_competitor: { label: "Competidor rechazado", tone: "error" },
  historic_duplicate: { label: "Duplicado histórico", tone: "neutral" },
  pending: { label: "Pendiente", tone: "info" },
};
const RELATION: Record<string, { label: string; tone: BadgeTone }> = {
  independent: { label: "Independiente", tone: "neutral" },
  competing: { label: "Competidor", tone: "warning" },
  conflict_resolved: { label: "Conflicto resuelto", tone: "success" },
  rejected_competitor: { label: "Competidor rechazado", tone: "error" },
  superseded_exact_duplicate: { label: "Duplicado histórico", tone: "neutral" },
};
const MAPPING_STATUS: Record<string, string> = {
  auto_approved: "Autoaprobado",
  manually_approved: "Aprobado manualmente",
  candidate: "Pendiente",
  ambiguous: "Ambiguo",
  rejected: "Rechazado",
  incompatible: "Incompatible",
};

function StatusBadges({ c }: { c: MappingCandidate }) {
  const life = LIFECYCLE[c.lifecycle_status] ?? { label: c.lifecycle_status, tone: "neutral" };
  const rel = RELATION[c.relation_status] ?? { label: c.relation_status, tone: "neutral" };
  return (
    <div className="flex flex-wrap gap-1">
      <Badge tone={life.tone}>{MAPPING_STATUS[c.mapping_status] ?? life.label}</Badge>
      <Badge tone={rel.tone}>{rel.label}</Badge>
      {c.exclusion_warning ? <Badge tone="error">Término excluyente</Badge> : null}
    </div>
  );
}

export default function IngredientMappingsPage() {
  const [provider, setProvider] = useState<string>("parsebot-alcampo");
  const [ingredient, setIngredient] = useState("");
  const [status, setStatus] = useState("");
  const [relation, setRelation] = useState("");
  const [minConfidence, setMinConfidence] = useState("");
  const [includeHistoric, setIncludeHistoric] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [detailId, setDetailId] = useState<number | null>(null);
  const [conflictGroup, setConflictGroup] = useState<string | null>(null);

  const filters: MappingCandidateFilters = useMemo(
    () => ({
      provider_code: provider,
      canonical_ingredient_key: ingredient || undefined,
      mapping_status: status || undefined,
      relation_status: relation || undefined,
      minimum_confidence: minConfidence ? Number(minConfidence) : undefined,
      include_historic: includeHistoric || undefined,
      conflict_group_id: conflictGroup || undefined,
      limit: 50,
    }),
    [provider, ingredient, status, relation, minConfidence, includeHistoric, conflictGroup],
  );

  const list = useMappingCandidatesQuery(filters);
  const summary = useMappingSummaryQuery(provider);
  const actions = useMappingActions();
  const items = list.data?.items ?? [];
  const detail = items.find((i) => i.mapping_id === detailId) ?? null;

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const askReason = (verb: string): string | null => {
    const reason = window.prompt(`Motivo de ${verb}:`);
    return reason && reason.trim() ? reason.trim() : null;
  };

  const onReject = (id: number) => {
    const reason = askReason("rechazo");
    if (reason) actions.reject.mutate({ id, reason });
  };
  const onRevoke = (id: number) => {
    const reason = askReason("revocación");
    if (reason) actions.revoke.mutate({ id, reason });
  };
  const onBulkReject = () => {
    const reason = askReason("rechazo masivo");
    if (reason) actions.bulkReject.mutate({ ids: [...selected], reason }, { onSuccess: () => setSelected(new Set()) });
  };
  const onBulkApprove = () =>
    actions.bulkApprove.mutate({ ids: [...selected] }, { onSuccess: () => setSelected(new Set()) });

  const busy =
    actions.approve.isPending ||
    actions.reject.isPending ||
    actions.revoke.isPending ||
    actions.enrich.isPending ||
    actions.bulkApprove.isPending ||
    actions.bulkReject.isPending;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="font-display text-display-lg text-ink">Mapeos ingrediente↔producto</h1>
        <p className="mt-1 text-ink-muted">
          Cola interna de revisión. Aprueba, rechaza, revoca o enriquece candidatos.
        </p>
      </div>

      <Alert tone="info">{REVIEW_NOTICE}</Alert>

      {summary.data ? (
        <Card>
          <CardHeader>
            <CardTitle>Resumen · {provider}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            <div className="flex flex-wrap gap-4 text-ink-muted">
              <span>Productos únicos: {summary.data.unique_products_discovered}</span>
              <span>Candidatos: {summary.data.candidate_pairs}</span>
              <span>Grupos de conflicto: {summary.data.competing_candidate_groups}</span>
              <span>Sin resolver: {summary.data.unresolved_conflict_groups}</span>
              <span>
                Presupuesto enriquecimiento: {summary.data.enrichment_budget.used}/
                {summary.data.enrichment_budget.budget}
              </span>
            </div>
            <div className="flex flex-wrap gap-4 text-ink-muted">
              <span>pair_ratio: {summary.data.candidate_pair_ratio}</span>
              <span>multi_ingr_ratio: {summary.data.multi_ingredient_product_ratio}</span>
              <span>avg/grupo: {summary.data.average_candidates_per_conflict_group}</span>
            </div>
            {summary.data.explosion_state === "critical" ? (
              <Alert tone="error">
                Autoaprobación bloqueada por explosión crítica de candidatos.
              </Alert>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {/* Filters */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Select
          label="Proveedor"
          options={PROVIDERS.map((p) => ({ value: p, label: p }))}
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        />
        <Input
          label="Ingrediente"
          placeholder="aceite_oliva"
          value={ingredient}
          onChange={(e) => setIngredient(e.target.value)}
        />
        <Select
          label="Estado"
          options={[
            { value: "", label: "Todos" },
            { value: "candidate", label: "Pendiente" },
            { value: "ambiguous", label: "Ambiguo" },
            { value: "auto_approved", label: "Autoaprobado" },
            { value: "manually_approved", label: "Aprobado manualmente" },
            { value: "rejected", label: "Rechazado" },
          ]}
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        />
        <Select
          label="Relación"
          options={[
            { value: "", label: "Todas" },
            { value: "competing", label: "Competidor" },
            { value: "conflict_resolved", label: "Conflicto resuelto" },
            { value: "rejected_competitor", label: "Competidor rechazado" },
          ]}
          value={relation}
          onChange={(e) => setRelation(e.target.value)}
        />
        <Input
          label="Confianza mínima"
          type="number"
          step="0.05"
          min="0"
          max="1"
          value={minConfidence}
          onChange={(e) => setMinConfidence(e.target.value)}
        />
        <label className="flex items-center gap-2 self-end text-sm text-ink-muted">
          <input
            type="checkbox"
            checked={includeHistoric}
            onChange={(e) => setIncludeHistoric(e.target.checked)}
          />
          Incluir duplicados históricos
        </label>
        {conflictGroup ? (
          <Button variant="outline" size="sm" onClick={() => setConflictGroup(null)}>
            Salir del conflicto {conflictGroup}
          </Button>
        ) : null}
      </div>

      {selected.size > 0 ? (
        <div className="flex items-center gap-3 rounded-md border border-border bg-bg-subtle px-4 py-2">
          <span className="text-sm text-ink">{selected.size} seleccionados</span>
          <Button size="sm" variant="primary" disabled={busy} onClick={onBulkApprove}>
            Aprobar en masa
          </Button>
          <Button size="sm" variant="danger" disabled={busy} onClick={onBulkReject}>
            Rechazar en masa
          </Button>
          {actions.bulkApprove.isError ? (
            <span className="text-sm text-error">Bloqueado: candidatos no elegibles.</span>
          ) : null}
        </div>
      ) : null}

      {/* Table */}
      {list.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : list.isError ? (
        <Alert tone="error">No se pudo cargar la cola de revisión.</Alert>
      ) : items.length === 0 ? (
        <Alert tone="info">No hay candidatos con estos filtros.</Alert>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="bg-bg-subtle text-left text-ink-muted">
              <tr>
                <th className="p-2" />
                <th className="p-2">Ingrediente</th>
                <th className="p-2">Producto</th>
                <th className="p-2">Confianza</th>
                <th className="p-2">Costeo</th>
                <th className="p-2">Estado</th>
                <th className="p-2">Desbloquea</th>
                <th className="p-2">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.mapping_id} className="border-t border-border align-top">
                  <td className="p-2">
                    <input
                      type="checkbox"
                      checked={selected.has(c.mapping_id)}
                      onChange={() => toggle(c.mapping_id)}
                      aria-label={`Seleccionar ${c.mapping_id}`}
                    />
                  </td>
                  <td className="p-2 font-medium text-ink">{c.canonical_ingredient_key}</td>
                  <td className="p-2 text-ink-muted">
                    {c.original_product_name ?? c.external_product_id}
                    {c.conflict_group_id ? (
                      <button
                        className="ml-1 text-accent underline"
                        onClick={() => setConflictGroup(c.conflict_group_id)}
                      >
                        conflicto
                      </button>
                    ) : null}
                  </td>
                  <td className="p-2">{c.confidence_score}</td>
                  <td className="p-2">{c.product_costing_mode ?? "—"}</td>
                  <td className="p-2">
                    <StatusBadges c={c} />
                  </td>
                  <td className="p-2 text-center">{c.recipes_potentially_unlocked}</td>
                  <td className="p-2">
                    <div className="flex flex-wrap gap-1">
                      <Button size="sm" variant="ghost" onClick={() => setDetailId(c.mapping_id)}>
                        Ver
                      </Button>
                      <Button
                        size="sm"
                        variant="primary"
                        disabled={busy}
                        onClick={() => actions.approve.mutate({ id: c.mapping_id })}
                      >
                        Aprobar
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => actions.enrich.mutate(c.mapping_id)}
                      >
                        Enriquecer
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={busy}
                        onClick={() => onReject(c.mapping_id)}
                      >
                        Rechazar
                      </Button>
                      {c.lifecycle_status === "approved" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => onRevoke(c.mapping_id)}
                        >
                          Revocar
                        </Button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detail ? (
        <Card>
          <CardHeader>
            <CardTitle>
              Detalle · {detail.canonical_ingredient_key} #{detail.mapping_id}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm text-ink-muted">
            <StatusBadges c={detail} />
            <p>Producto: {detail.original_product_name ?? detail.external_product_id}</p>
            <p>
              Scores — léxico {detail.lexical_score}, semántico {detail.semantic_score ?? "—"},
              categoría {detail.category_score}, confianza {detail.confidence_score}
            </p>
            <p>
              Envase {detail.net_content ?? "—"} · precio {detail.price ?? "—"} · unit_price{" "}
              {detail.unit_price ?? "—"}/{detail.unit_price_unit ?? "—"} · costeo{" "}
              {detail.product_costing_mode ?? "—"} ({detail.costing_eligible ? "costeable" : "no"})
            </p>
            <p>reviewable: {String(detail.reviewable)} · selectable_for_costing: {String(detail.selectable_for_costing)}</p>
            <p>Enriquecimiento: {detail.enrichment_status}
              {detail.enrichment_error_category ? ` (${detail.enrichment_error_category})` : ""}</p>
            {detail.warnings.length ? <p>Advertencias: {detail.warnings.join("; ")}</p> : null}
            {detail.enriched_fields ? (
              <p>Enriquecido: {Object.keys(detail.enriched_fields).join(", ")}</p>
            ) : null}
            <div className="flex gap-2">
              <Button size="sm" variant="ghost" onClick={() => setDetailId(null)}>
                Cerrar
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
