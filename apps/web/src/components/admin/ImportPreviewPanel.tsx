import { importStatusLabel, importStatusTone } from "@/lib/domain/labels";
import type { AdminImportRecord } from "@/lib/api/types";

import { Badge } from "@/components/ui/Badge";
import { KeyValueGrid } from "@/components/admin/KeyValueGrid";
import { ImportErrorsTable } from "@/components/admin/ImportErrorsTable";

export function ImportPreviewPanel({ record }: { record: AdminImportRecord }) {
  const errors = record.errors ?? [];

  return (
    <div className="flex flex-col gap-4 rounded-md border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-display text-display-sm text-ink">{record.filename ?? "Vista previa"}</p>
        <Badge tone={importStatusTone(record)}>{importStatusLabel(record)}</Badge>
      </div>

      <div>
        <p className="mb-1.5 text-sm font-semibold text-ink">Recuento</p>
        <KeyValueGrid data={record.counts} emptyMessage="La API no devolvió un recuento." />
      </div>

      {errors.length > 0 ? (
        <ImportErrorsTable errors={errors} />
      ) : (
        <p className="text-sm font-medium text-success">Sin errores de validación.</p>
      )}

      {record.would_change ? (
        <div>
          <p className="mb-1.5 text-sm font-semibold text-ink">Cambios previstos</p>
          <KeyValueGrid data={record.would_change} emptyMessage="No se anticipan cambios." />
        </div>
      ) : null}
    </div>
  );
}
