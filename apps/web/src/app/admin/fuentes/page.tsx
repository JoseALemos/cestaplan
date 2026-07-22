"use client";

import { useAdminSourcesQuery } from "@/lib/query/hooks/use-admin";

import { Alert } from "@/components/ui/Alert";
import { Skeleton } from "@/components/ui/Skeleton";
import { SourceCard } from "@/components/admin/SourceCard";

export default function FuentesPage() {
  const sourcesQuery = useAdminSourcesQuery();

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Estado de fuentes</h1>
        <p className="mt-1 text-ink-muted">
          Cada adaptador de datos registrado, su licencia y atribución, y la última importación
          recibida.
        </p>
      </div>

      {sourcesQuery.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : sourcesQuery.isError ? (
        <Alert tone="error">No se pudieron cargar las fuentes de datos.</Alert>
      ) : (sourcesQuery.data ?? []).length === 0 ? (
        <Alert tone="info">Todavía no hay ninguna fuente registrada.</Alert>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {sourcesQuery.data?.map((source) => (
            <SourceCard key={source.adapter_key} source={source} />
          ))}
        </div>
      )}
    </div>
  );
}
