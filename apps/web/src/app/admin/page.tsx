"use client";

import Link from "next/link";

import { importStatusLabel, importStatusTone } from "@/lib/domain/labels";
import { useAdminImportsQuery, useAdminSourcesQuery } from "@/lib/query/hooks/use-admin";
import { formatDateTime } from "@/lib/utils/format";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Skeleton } from "@/components/ui/Skeleton";

function StatTile({ label, value, tone }: { label: string; value: number; tone?: "success" | "warning" | "neutral" }) {
  const toneClass =
    tone === "success" ? "text-success" : tone === "warning" ? "text-warning" : "text-ink";
  return (
    <div className="rounded-md border border-border bg-surface p-4">
      <p className="text-xs text-ink-muted">{label}</p>
      <p className={`font-display text-display-lg ${toneClass}`}>{value}</p>
    </div>
  );
}

export default function AdminDashboardPage() {
  const sourcesQuery = useAdminSourcesQuery();
  const importsQuery = useAdminImportsQuery();

  const sources = sourcesQuery.data ?? [];
  const enabledCount = sources.filter((source) => source.enabled).length;
  const disabledCount = sources.length - enabledCount;
  const communityCount = sources.filter((source) => source.is_community).length;

  const recentImports = (importsQuery.data ?? []).slice(0, 5);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Administración de catálogos</h1>
        <p className="mt-1 text-ink-muted">
          Gestiona la importación de precios y productos, y comprueba el estado de cada fuente
          de datos.
        </p>
      </div>

      {sourcesQuery.isLoading ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : sourcesQuery.isError ? (
        <Alert tone="error">No se pudo cargar el estado de las fuentes.</Alert>
      ) : (
        <div className="grid gap-3 sm:grid-cols-3">
          <StatTile label="Fuentes activas" value={enabledCount} tone="success" />
          <StatTile label="Fuentes desactivadas" value={disabledCount} tone={disabledCount > 0 ? "warning" : "neutral"} />
          <StatTile label="Fuentes de comunidad" value={communityCount} />
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Importación de catálogo</CardTitle>
            <CardDescription>
              Sube un CSV o JSON con precios y productos de una tienda. Siempre se valida en
              modo vista previa antes de escribir nada.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link href="/admin/importacion">
              <Button size="sm">Ir a importación</Button>
            </Link>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Estado de fuentes</CardTitle>
            <CardDescription>
              Adaptadores registrados, su licencia y atribución, y la última importación
              recibida.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link href="/admin/fuentes">
              <Button size="sm" variant="outline">
                Ver fuentes
              </Button>
            </Link>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Importaciones recientes</CardTitle>
          <CardDescription>Las 5 últimas, más recientes primero.</CardDescription>
        </CardHeader>
        <CardContent>
          {importsQuery.isLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : importsQuery.isError ? (
            <Alert tone="error">No se pudo cargar el historial de importaciones.</Alert>
          ) : recentImports.length === 0 ? (
            <p className="text-sm text-ink-muted">Todavía no se ha realizado ninguna importación.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {recentImports.map((item) => (
                <li key={item.id}>
                  <Link
                    href={`/admin/importacion/${item.id}`}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3.5 py-2.5 text-sm hover:bg-bg-subtle"
                  >
                    <span className="min-w-0 truncate font-medium text-ink">
                      {item.filename ?? item.id}
                    </span>
                    <span className="flex items-center gap-2 text-ink-muted">
                      {formatDateTime(item.created_at)}
                      <Badge tone={importStatusTone(item)}>{importStatusLabel(item)}</Badge>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Enriquecimiento por código de barras</CardTitle>
          <CardDescription>Próximamente.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Alert tone="info">
            Esta API todavía no expone un endpoint de enriquecimiento (Open Food Facts u otro)
            en <code className="font-mono text-xs">/openapi.json</code>. En cuanto esté
            disponible, este panel permitirá buscar un producto por su código de barras y
            aplicar su ficha y atribución directamente al catálogo.
          </Alert>
          <div className="flex flex-wrap items-end gap-3 opacity-60">
            <Input label="Código de barras (EAN/UPC)" placeholder="8412345000011" disabled className="max-w-xs" />
            <Button type="button" size="sm" disabled>
              Buscar producto
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
