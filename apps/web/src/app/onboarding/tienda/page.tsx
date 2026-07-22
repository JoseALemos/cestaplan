"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { formatDate } from "@/lib/utils/format";
import { formatCoveragePercent } from "@/lib/domain/labels";
import type { BadgeTone } from "@/components/ui/Badge";
import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { useRetailersQuery, useStoresQuery } from "@/lib/query/hooks/use-catalog";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";

export default function TiendaPage() {
  const router = useRouter();
  const { state, setStore } = useOnboarding();

  const retailersQuery = useRetailersQuery();
  const [retailerId, setRetailerId] = useState<string>(state.store?.retailerId ?? "");
  const storesQuery = useStoresQuery(retailerId || undefined);

  const [storeId, setStoreId] = useState<string>(state.store?.storeId ?? "");
  const selectedStore = useMemo(
    () => storesQuery.data?.find((store) => store.id === storeId),
    [storesQuery.data, storeId],
  );

  const retailerOptions = (retailersQuery.data ?? []).map((retailer) => ({
    value: retailer.id,
    label: retailer.is_synthetic ? `${retailer.name} (datos sintéticos)` : retailer.name,
  }));
  const storeOptions = (storesQuery.data ?? []).map((store) => ({
    value: store.id,
    label: `${store.name} — ${store.locality} (${store.postal_code})`,
  }));

  const continueDisabled = false; // el vínculo hogar↔tienda todavía no existe en la API; nunca bloquea el alta.

  const coverageRatio = selectedStore?.price_coverage
    ? Number.parseFloat(selectedStore.price_coverage)
    : null;
  const coverageBadgeTone: BadgeTone =
    coverageRatio === null ? "neutral" : coverageRatio >= 0.9 ? "success" : coverageRatio >= 0.5 ? "warning" : "error";

  const onContinue = () => {
    setStore({
      retailerId: retailerId || null,
      storeId: storeId || null,
      storeLabel: selectedStore?.name ?? null,
      province: selectedStore?.province ?? null,
      postalCode: selectedStore?.postal_code ?? null,
    });
    router.push("/onboarding/miembros");
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Selección de tienda</CardTitle>
        <CardDescription>
          Cadena y tienda concreta. Verás la cobertura de precios antes de continuar.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {retailersQuery.isLoading ? (
          <Skeleton className="h-11 w-full" />
        ) : retailersQuery.isError ? (
          <Alert tone="warning" title="Catálogo de tiendas no disponible todavía">
            Esta parte de la API aún no está publicada. Puedes continuar el alta sin elegir
            tienda; te lo pediremos más adelante.
          </Alert>
        ) : retailerOptions.length === 0 ? (
          <Alert tone="info">Todavía no hay cadenas dadas de alta.</Alert>
        ) : (
          <Select
            label="Cadena"
            placeholder="Selecciona una cadena"
            options={retailerOptions}
            value={retailerId}
            onChange={(event) => {
              setRetailerId(event.target.value);
              setStoreId("");
            }}
          />
        )}

        {retailerId ? (
          storesQuery.isLoading ? (
            <Skeleton className="h-11 w-full" />
          ) : storesQuery.isError ? (
            <Alert tone="warning">No se pudieron cargar las tiendas de esta cadena.</Alert>
          ) : storeOptions.length === 0 ? (
            <Alert tone="info">Esta cadena todavía no tiene tiendas dadas de alta.</Alert>
          ) : (
            <Select
              label="Tienda"
              placeholder="Selecciona una tienda"
              options={storeOptions}
              value={storeId}
              onChange={(event) => setStoreId(event.target.value)}
            />
          )
        ) : null}

        {selectedStore ? (
          <div className="flex flex-col gap-2 rounded-md border border-border px-4 py-3 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-ink-muted">Provincia / localidad</span>
              <span className="font-medium text-ink">
                {selectedStore.province} · {selectedStore.locality}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-muted">Código postal</span>
              <span className="font-medium text-ink">{selectedStore.postal_code}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-muted">Catálogo actualizado</span>
              <span className="font-medium text-ink">
                {formatDate(selectedStore.catalog_updated_at)}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-muted">Cobertura de precios</span>
              <Badge tone={coverageBadgeTone}>{formatCoveragePercent(selectedStore.price_coverage)}</Badge>
            </div>
          </div>
        ) : null}
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/hogar")}>
          Atrás
        </Button>
        <Button type="button" size="sm" disabled={continueDisabled} onClick={onContinue}>
          Continuar
        </Button>
      </div>
    </Card>
  );
}
