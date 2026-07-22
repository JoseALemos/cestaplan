"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useId, useRef, useState } from "react";
import { useForm } from "react-hook-form";

import { importMappingSchema, type ImportMappingFormValues } from "@/lib/admin/schemas";
import { cn } from "@/lib/utils/cn";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";

const EXPECTED_COLUMNS = [
  "retailer_slug",
  "store_external_code",
  "store_province",
  "store_locality",
  "store_postal_code",
  "product_external_id",
  "product_name",
  "brand",
  "category",
  "barcode",
  "package_quantity",
  "package_unit",
  "amount",
  "currency",
  "unit_price",
  "promotion",
  "availability",
  "source_type",
  "source_name",
  "source_url",
  "observed_at",
  "expires_at",
  "confidence_score",
  "verification_status",
];

const ACCEPTED_EXTENSIONS = [".csv", ".json"];

function isAcceptedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension));
}

export interface ImportUploadFormProps {
  onSubmitPreview: (file: File, columnMapping: string | undefined) => Promise<void>;
  pending: boolean;
}

export function ImportUploadForm({ onSubmitPreview, pending }: ImportUploadFormProps) {
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [mappingOpen, setMappingOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dropzoneLabelId = useId();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<ImportMappingFormValues>({
    resolver: zodResolver(importMappingSchema),
    defaultValues: { column_mapping: "" },
  });

  function pickFile(candidate: File | undefined | null) {
    if (!candidate) return;
    if (!isAcceptedFile(candidate)) {
      setFile(null);
      setFileError("Solo se admiten archivos .csv o .json.");
      return;
    }
    setFile(candidate);
    setFileError(null);
  }

  const onSubmit = handleSubmit(async (values) => {
    if (!file) {
      setFileError("Selecciona un archivo CSV o JSON para continuar.");
      return;
    }
    await onSubmitPreview(file, values.column_mapping || undefined);
  });

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      <div>
        <p id={dropzoneLabelId} className="text-sm font-medium text-ink">
          Archivo de precios y productos
          <span aria-hidden="true" className="ml-0.5 text-accent-strong">
            *
          </span>
        </p>
        <div
          role="button"
          tabIndex={0}
          aria-labelledby={dropzoneLabelId}
          onClick={() => fileInputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              fileInputRef.current?.click();
            }
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setIsDragging(false);
            pickFile(event.dataTransfer.files?.[0]);
          }}
          className={cn(
            "mt-1.5 flex cursor-pointer flex-col items-center gap-2 rounded-lg border-2 border-dashed px-6 py-8 text-center transition-colors duration-fast",
            isDragging ? "border-accent bg-accent-soft" : "border-border-strong bg-bg-subtle",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-focus-ring)]",
          )}
        >
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true" className="text-ink-faint">
            <path
              d="M12 15V4m0 0L7.5 8.5M12 4l4.5 4.5"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <path
              d="M4 15v3a2 2 0 002 2h12a2 2 0 002-2v-3"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
          {file ? (
            <p className="text-sm font-medium text-ink">{file.name}</p>
          ) : (
            <>
              <p className="text-sm font-medium text-ink">Arrastra tu CSV o JSON aquí</p>
              <p className="text-xs text-ink-muted">o haz clic para elegir un archivo</p>
            </>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.json,text/csv,application/json"
            className="sr-only"
            onChange={(event) => pickFile(event.target.files?.[0])}
          />
        </div>
        {fileError ? (
          <p role="alert" className="mt-1.5 text-xs font-medium text-error">
            {fileError}
          </p>
        ) : (
          <p className="mt-1.5 text-xs text-ink-muted">
            Formatos admitidos: CSV o JSON.{" "}
            <a href="/plantillas/importacion-ejemplo.csv" download className="font-medium text-primary hover:underline">
              Descargar plantilla / ejemplo
            </a>
          </p>
        )}
      </div>

      <details className="rounded-md border border-border p-3.5">
        <summary className="cursor-pointer text-sm font-medium text-ink">
          Columnas esperadas del CSV
        </summary>
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {EXPECTED_COLUMNS.map((column) => (
            <code
              key={column}
              className="rounded bg-bg-subtle px-1.5 py-0.5 font-mono text-xs text-ink-muted"
            >
              {column}
            </code>
          ))}
        </div>
      </details>

      <div>
        <button
          type="button"
          onClick={() => setMappingOpen((open) => !open)}
          aria-expanded={mappingOpen}
          className="text-sm font-medium text-primary hover:underline"
        >
          {mappingOpen ? "Ocultar mapeo de columnas avanzado" : "¿Tus columnas tienen otros nombres? Añade un mapeo"}
        </button>
        {mappingOpen ? (
          <div className="mt-2 flex flex-col gap-1.5">
            <label htmlFor="column_mapping" className="text-sm font-medium text-ink">
              Mapeo de columnas (JSON opcional)
            </label>
            <textarea
              id="column_mapping"
              rows={3}
              placeholder='{"Nombre del producto": "product_name", "PVP": "amount"}'
              aria-invalid={Boolean(errors.column_mapping) || undefined}
              aria-describedby={errors.column_mapping ? "column_mapping-error" : undefined}
              className={cn(
                "w-full rounded-md border border-border bg-surface px-3.5 py-2.5 font-mono text-sm text-ink placeholder:text-ink-faint",
                "transition-colors duration-fast focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-focus-ring)] focus-visible:border-transparent",
                errors.column_mapping && "border-error focus-visible:ring-error",
              )}
              {...register("column_mapping")}
            />
            {errors.column_mapping ? (
              <p id="column_mapping-error" role="alert" className="text-xs font-medium text-error">
                {errors.column_mapping.message}
              </p>
            ) : (
              <p className="text-xs text-ink-muted">
                Asocia el nombre de columna de tu archivo con el nombre canónico esperado.
              </p>
            )}
          </div>
        ) : null}
      </div>

      <Alert tone="info">
        Al enviar se ejecuta primero una <strong>vista previa</strong> (dry-run): valida el
        archivo y muestra qué cambiaría, sin escribir nada todavía.
      </Alert>

      <Button type="submit" loading={pending} className="self-start">
        Generar vista previa
      </Button>
    </form>
  );
}
