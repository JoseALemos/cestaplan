import { sourceStatusLabel } from "@/lib/domain/labels";
import { cn } from "@/lib/utils/cn";
import type { AdminSource } from "@/lib/api/types";

import { Badge } from "@/components/ui/Badge";
import { Card, CardHeader } from "@/components/ui/Card";
import { KeyValueGrid } from "@/components/admin/KeyValueGrid";

const CAPABILITY_LABELS: Record<string, string> = {
  search: "Búsqueda",
  get_product: "Ficha de producto",
  get_price: "Precio",
  get_availability: "Disponibilidad",
  store_catalog: "Catálogo de tienda",
};

function formatRetailerEntry(entry: unknown): string {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object") {
    const record = entry as Record<string, unknown>;
    const label = record.name ?? record.slug ?? record.retailer_slug ?? record.id;
    if (typeof label === "string") return label;
  }
  return JSON.stringify(entry);
}

export function SourceCard({ source }: { source: AdminSource }) {
  const capabilities = Object.entries(source.capabilities ?? {}).filter(([, value]) => value !== undefined);
  const retailers = source.retailers ?? [];

  return (
    <Card
      className={cn(
        !source.enabled && "border-dashed bg-bg-subtle opacity-90",
      )}
    >
      <CardHeader className="mb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="font-display text-display-sm text-ink">{source.adapter_key}</p>
            <p className="text-xs text-ink-muted">
              v{source.version} · {source.source_type}
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-1.5">
            <Badge tone={source.enabled ? "success" : "neutral"}>
              {source.enabled ? "Activo" : "Desactivado"}
            </Badge>
            {source.is_community ? <Badge tone="warning">Comunidad</Badge> : null}
            {source.requires_network ? (
              <Badge tone="info">Requiere red</Badge>
            ) : (
              <Badge tone="neutral">Sin conexión externa</Badge>
            )}
          </div>
        </div>
        <p className="text-xs text-ink-faint">Estado: {sourceStatusLabel(source.status)}</p>
      </CardHeader>

      <div className="flex flex-col gap-3 text-sm">
        {capabilities.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {capabilities.map(([key, value]) => (
              <Badge key={key} tone={value ? "primary" : "neutral"} className={!value ? "opacity-50" : undefined}>
                {value ? "✓ " : "✗ "}
                {CAPABILITY_LABELS[key] ?? key}
              </Badge>
            ))}
          </div>
        ) : null}

        {retailers.length > 0 ? (
          <p className="text-xs text-ink-muted">
            Tiendas: {retailers.map((entry) => formatRetailerEntry(entry)).join(", ")}
          </p>
        ) : null}

        <div className="rounded-md border border-primary/25 bg-primary-soft px-3.5 py-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-primary">
            Licencia y atribución
          </p>
          <p className="mt-1 text-sm font-medium text-ink">
            {source.license_code ?? "Licencia no especificada"}
          </p>
          {source.attribution_text ? (
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">{source.attribution_text}</p>
          ) : (
            <p className="mt-1 text-xs italic text-ink-faint">Sin texto de atribución declarado.</p>
          )}
        </div>

        {source.last_import ? (
          <div>
            <p className="mb-1 text-xs font-semibold text-ink">Última importación</p>
            <KeyValueGrid data={source.last_import} />
          </div>
        ) : (
          <p className="text-xs text-ink-faint">Sin importaciones registradas para esta fuente.</p>
        )}

        {source.coverage ? (
          <div>
            <p className="mb-1 text-xs font-semibold text-ink">Cobertura</p>
            <KeyValueGrid data={source.coverage} />
          </div>
        ) : null}
      </div>
    </Card>
  );
}
