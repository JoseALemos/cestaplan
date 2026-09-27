"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError } from "@/lib/api/client";
import {
  useAdminImportsQuery,
  useConfirmAndImportMutation,
  useCreateAdminImportMutation,
} from "@/lib/query/hooks/use-admin";
import type { AdminImportRecord } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { ImportHistoryList } from "@/components/admin/ImportHistoryList";
import { ImportPreviewPanel } from "@/components/admin/ImportPreviewPanel";
import { ImportUploadForm } from "@/components/admin/ImportUploadForm";

function describeApiError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 422) return "El archivo no tiene un formato válido. Revisa las columnas esperadas.";
    if (error.status === 403) return "Tu sesión ya no tiene permisos de administrador.";
    return `La API respondió con un error (${error.status}).`;
  }
  return "No se pudo conectar con la API. Comprueba tu conexión.";
}

export default function ImportacionPage() {
  const router = useRouter();
  const { showToast } = useToast();
  const importsQuery = useAdminImportsQuery();
  const previewMutation = useCreateAdminImportMutation();
  const confirmMutation = useConfirmAndImportMutation();

  const [preview, setPreview] = useState<AdminImportRecord | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingMapping, setPendingMapping] = useState<string | undefined>(undefined);
  // F6: confirmación inline (sistema de diseño) en vez del window.confirm nativo.
  const [awaitingConfirm, setAwaitingConfirm] = useState(false);

  const errorCount = preview?.errors?.length ?? 0;

  function resetPreview() {
    setPreview(null);
    setPendingFile(null);
    setAwaitingConfirm(false);
  }

  async function handlePreview(file: File, columnMapping: string | undefined) {
    setPreview(null);
    try {
      const record = await previewMutation.mutateAsync({ file, dry_run: true, column_mapping: columnMapping });
      setPreview(record);
      setPendingFile(file);
      setPendingMapping(columnMapping);
    } catch (error) {
      showToast({ tone: "error", title: "No se pudo generar la vista previa", description: describeApiError(error) });
    }
  }

  function handleConfirm() {
    if (!pendingFile) return;
    // Con errores, pedimos confirmación inline (diálogo propio) antes de importar solo lo válido.
    if (errorCount > 0) {
      setAwaitingConfirm(true);
      return;
    }
    void runImport();
  }

  async function runImport() {
    if (!pendingFile) return;
    setAwaitingConfirm(false);
    try {
      const committed = await confirmMutation.mutateAsync({ file: pendingFile, column_mapping: pendingMapping });
      showToast({ tone: "success", title: "Importación aplicada" });
      setPreview(null);
      setPendingFile(null);
      router.push(`/admin/importacion/${committed.id}`);
    } catch (error) {
      showToast({ tone: "error", title: "No se pudo confirmar la importación", description: describeApiError(error) });
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Importación de catálogo</h1>
        <p className="mt-1 text-ink-muted">
          Sube precios y productos de una tienda. Primero se valida en vista previa; nada se
          escribe hasta que confirmes.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>1. Subir archivo</CardTitle>
          <CardDescription>Formatos CSV o JSON. Se ejecuta primero en modo vista previa.</CardDescription>
        </CardHeader>
        <CardContent>
          <ImportUploadForm onSubmitPreview={handlePreview} pending={previewMutation.isPending} />
        </CardContent>
      </Card>

      {previewMutation.isPending ? (
        <Card>
          <CardContent>
            <Skeleton className="h-40 w-full" />
          </CardContent>
        </Card>
      ) : preview ? (
        <Card>
          <CardHeader>
            <CardTitle>2. Vista previa</CardTitle>
            <CardDescription>Revisa el recuento y los errores antes de confirmar.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <ImportPreviewPanel record={preview} />
            {errorCount > 0 ? (
              <Alert tone="warning">
                Se encontraron errores en {errorCount} fila(s). Puedes corregir el archivo y
                volver a subirlo, o confirmar para importar únicamente las filas válidas.
              </Alert>
            ) : null}
            {awaitingConfirm ? (
              <div className="flex flex-col gap-3" role="group" aria-label="Confirmar importación con errores">
                <Alert tone="warning">
                  La vista previa encontró {errorCount} error(es). Si continúas se importarán solo
                  las filas válidas y las erróneas se omitirán. ¿Quieres continuar?
                </Alert>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    loading={confirmMutation.isPending}
                    onClick={() => void runImport()}
                  >
                    Importar solo las válidas
                  </Button>
                  <Button type="button" variant="ghost" onClick={() => setAwaitingConfirm(false)}>
                    Cancelar
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-wrap gap-2">
                <Button type="button" loading={confirmMutation.isPending} onClick={handleConfirm}>
                  Confirmar e importar
                </Button>
                <Button type="button" variant="ghost" onClick={resetPreview}>
                  Descartar vista previa
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Historial de importaciones</CardTitle>
          <CardDescription>Más recientes primero. Abre una para ver sus errores o revertirla.</CardDescription>
        </CardHeader>
        <CardContent>
          {importsQuery.isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : importsQuery.isError ? (
            <Alert tone="error">No se pudo cargar el historial de importaciones.</Alert>
          ) : (
            <ImportHistoryList imports={importsQuery.data ?? []} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
