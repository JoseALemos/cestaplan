"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { ApiError } from "@/lib/api/client";
import { importStatusLabel, importStatusTone } from "@/lib/domain/labels";
import { useAdminImportQuery, useRollbackAdminImportMutation } from "@/lib/query/hooks/use-admin";
import { formatDateTime } from "@/lib/utils/format";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { ImportErrorsTable } from "@/components/admin/ImportErrorsTable";
import { KeyValueGrid } from "@/components/admin/KeyValueGrid";

function describeApiError(error: unknown): string {
  if (error instanceof ApiError) return `La API respondió con un error (${error.status}).`;
  return "No se pudo conectar con la API. Comprueba tu conexión.";
}

export default function ImportDetailPage() {
  const params = useParams<{ importId: string }>();
  const importId = params.importId;
  const { showToast } = useToast();

  const importQuery = useAdminImportQuery(importId);
  const rollbackMutation = useRollbackAdminImportMutation();
  const [rollbackResult, setRollbackResult] = useState<number | null>(null);

  if (importQuery.isLoading) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (importQuery.isError || !importQuery.data) {
    return <Alert tone="error">No se pudo cargar el detalle de esta importación.</Alert>;
  }

  const record = importQuery.data;
  const errors = record.errors ?? [];
  const isCommitted = Boolean(record.committed_at) && !record.rolled_back_at;
  const isRolledBack = Boolean(record.rolled_back_at);
  const isPreviewOnly = record.dry_run && !record.committed_at;

  async function handleRollback() {
    const confirmed = window.confirm(
      "¿Revertir esta importación? Se eliminarán los precios que creó. Esta acción no se puede deshacer.",
    );
    if (!confirmed) return;
    try {
      const result = await rollbackMutation.mutateAsync(importId);
      const deleted = typeof result.deleted_prices === "number" ? result.deleted_prices : null;
      setRollbackResult(deleted);
      showToast({
        tone: "success",
        title: "Importación revertida",
        description: deleted !== null ? `${deleted} precio(s) eliminado(s).` : undefined,
      });
    } catch (error) {
      showToast({ tone: "error", title: "No se pudo revertir la importación", description: describeApiError(error) });
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href="/admin/importacion" className="text-sm font-medium text-primary hover:underline">
            ← Volver a importación
          </Link>
          <h1 className="mt-1 font-display text-display-lg text-ink">{record.filename ?? "Importación"}</h1>
          <p className="text-sm text-ink-muted">Creada el {formatDateTime(record.created_at)}</p>
        </div>
        <Badge tone={importStatusTone(record)}>{importStatusLabel(record)}</Badge>
      </div>

      {isPreviewOnly ? (
        <Alert tone="info">
          Esta es una vista previa (dry-run): no escribió ningún dato. Para aplicarla, vuelve a{" "}
          <Link href="/admin/importacion" className="underline">
            la pantalla de importación
          </Link>{" "}
          y vuelve a subir el archivo original hasta confirmar.
        </Alert>
      ) : null}

      {isRolledBack ? (
        <Alert tone="warning">
          Esta importación se revirtió el {formatDateTime(record.rolled_back_at)}.
          {rollbackResult !== null ? ` Se eliminaron ${rollbackResult} precio(s).` : ""}
        </Alert>
      ) : isCommitted ? (
        <Alert tone="success">Aplicada el {formatDateTime(record.committed_at)}.</Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Recuento</CardTitle>
          <CardDescription>Filas creadas, actualizadas u omitidas según la API.</CardDescription>
        </CardHeader>
        <CardContent>
          <KeyValueGrid data={record.counts} emptyMessage="La API no devolvió un recuento." />
        </CardContent>
      </Card>

      {record.would_change || record.summary ? (
        <Card>
          <CardHeader>
            <CardTitle>Resumen de cambios</CardTitle>
          </CardHeader>
          <CardContent>
            <KeyValueGrid data={record.would_change ?? record.summary} />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Errores de validación</CardTitle>
        </CardHeader>
        <CardContent>
          {errors.length === 0 ? (
            <p className="text-sm font-medium text-success">Sin errores de validación.</p>
          ) : (
            <ImportErrorsTable errors={errors} />
          )}
        </CardContent>
      </Card>

      {isCommitted ? (
        <Card>
          <CardHeader>
            <CardTitle>Revertir importación</CardTitle>
            <CardDescription>
              Elimina los precios que esta importación creó. Los productos y tiendas no se
              eliminan.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="danger" loading={rollbackMutation.isPending} onClick={handleRollback}>
              Revertir importación
            </Button>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
