"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { useRetailersQuery, useStoresQuery } from "@/lib/query/hooks/use-catalog";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";

export default function TiendaPage() {
  const router = useRouter();
  const { state, setStore } = useOnboarding();

  const retailersQuery = useRetailersQuery();
  const [retailerId, setRetailerId] = useState<string>(state.store?.retailerId ?? "");
  const selectedRetailer = useMemo(
    () => retailersQuery.data?.find((retailer) => retailer.id === retailerId),
    [retailersQuery.data, retailerId],
  );

  // Read-only context: how many of the chain's stores we aggregate prices from. The specific
  // store is irrelevant to pricing ("la tienda da igual"); this is purely informational.
  const storesQuery = useStoresQuery(retailerId || undefined);
  const storeCount = storesQuery.data?.length ?? null;

  const retailerOptions = (retailersQuery.data ?? []).map((retailer) => ({
    value: retailer.id,
    label: retailer.is_synthetic ? `${retailer.name} (datos sintéticos)` : retailer.name,
  }));

  const onContinue = () => {
    setStore({
      retailerId: retailerId || null,
      retailerLabel: selectedRetailer?.name ?? null,
    });
    router.push("/onboarding/miembros");
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Selección de cadena</CardTitle>
        <CardDescription>
          Elige la cadena; usaremos sus precios (de todas sus tiendas).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {retailersQuery.isLoading ? (
          <Skeleton className="h-11 w-full" />
        ) : retailersQuery.isError ? (
          <Alert tone="warning" title="Catálogo de cadenas no disponible todavía">
            Esta parte de la API aún no está publicada. Puedes continuar el alta sin elegir
            cadena; te lo pediremos más adelante.
          </Alert>
        ) : retailerOptions.length === 0 ? (
          <Alert tone="info">Todavía no hay cadenas dadas de alta.</Alert>
        ) : (
          <Select
            label="Cadena"
            placeholder="Selecciona una cadena"
            options={retailerOptions}
            value={retailerId}
            onChange={(event) => setRetailerId(event.target.value)}
          />
        )}

        {selectedRetailer ? (
          <div className="flex flex-col gap-2 rounded-md border border-border px-4 py-3 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-ink-muted">Cadena seleccionada</span>
              <span className="font-medium text-ink">{selectedRetailer.name}</span>
            </div>
            <p className="text-ink-muted">
              {storesQuery.isLoading
                ? "Cargando cobertura de la cadena…"
                : storeCount && storeCount > 0
                  ? `Tomaremos los precios más recientes de sus ${storeCount} ${
                      storeCount === 1 ? "tienda" : "tiendas"
                    } con datos. La tienda concreta da igual.`
                  : "Usaremos los precios más recientes de la cadena. La tienda concreta da igual."}
            </p>
          </div>
        ) : null}
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/hogar")}>
          Atrás
        </Button>
        <Button type="button" size="sm" onClick={onContinue}>
          Continuar
        </Button>
      </div>
    </Card>
  );
}
