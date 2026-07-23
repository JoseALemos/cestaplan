"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import type { PriceProvider } from "@/lib/api/types";
import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import {
  usePriceProvidersQuery,
  useRetailersQuery,
  useStoresQuery,
} from "@/lib/query/hooks/use-catalog";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";

// Badge → colour + a one-line honest caption. Mirrors the backend `_provider_badge`
// wording (spec §16): a chain is never dressed up as more than its real integration state.
const BADGE_STYLE: Record<string, { className: string; caption: string }> = {
  "Disponible para validación": {
    className: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
    caption: "Cobertura suficiente para costear planes (en validación, aún no en producción).",
  },
  Experimental: {
    className: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    caption:
      "Integración experimental: datos disponibles para validación, pero cobertura " +
      "insuficiente para calcular planes.",
  },
  "Ofertas solamente": {
    className: "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200",
    caption: "Solo folleto de ofertas: nunca es el precio completo de la tienda.",
  },
  "Configuración pendiente": {
    className: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
    caption: "Falta credencial o URL de captura; sin datos todavía.",
  },
  "Fuente insuficiente": {
    className: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200",
    caption: "La API responde pero su esquema no trae precio/envase suficientes.",
  },
  "Sin cobertura": {
    className: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
    caption: "No hay una fuente de precios disponible para esta cadena.",
  },
  "Bloqueado por autenticación": {
    className: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200",
    caption: "La fuente exige iniciar sesión del supermercado; no se envían credenciales.",
  },
};

const FALLBACK_BADGE = "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";

function ProviderBadge({ badge }: { badge: string }) {
  const style = BADGE_STYLE[badge]?.className ?? FALLBACK_BADGE;
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${style}`}>{badge}</span>
  );
}

// Honest, one-word description of what was actually OBSERVED — never the declared intent.
const OBSERVED_SCOPE_LABEL: Record<PriceProvider["observed_catalog_scope"], string> = {
  full: "Catálogo completo observado",
  partial: "Cobertura parcial observada",
  sample_only: "Solo muestra capturada",
  unknown: "Sin datos capturados",
};

function ChainStatusRow({ provider }: { provider: PriceProvider }) {
  const caption = BADGE_STYLE[provider.badge]?.caption ?? "";
  const scopeLabel = OBSERVED_SCOPE_LABEL[provider.observed_catalog_scope];
  // Never claim a chain can cost plans unless the measured eligibility says so.
  const costable =
    provider.costing_eligibility === "sufficient"
      ? "apta para costear planes"
      : "no apta para costear planes";
  return (
    <li className="flex items-start justify-between gap-3 py-2">
      <div className="min-w-0">
        <p className="font-medium text-ink capitalize">{provider.retailer}</p>
        <p className="text-xs text-ink-muted">
          {scopeLabel} · {costable}
        </p>
        <p className="text-xs text-ink-muted">{caption}</p>
      </div>
      <ProviderBadge badge={provider.badge} />
    </li>
  );
}

export default function TiendaPage() {
  const router = useRouter();
  const { state, setStore } = useOnboarding();

  const retailersQuery = useRetailersQuery();
  const providersQuery = usePriceProvidersQuery();
  const [retailerId, setRetailerId] = useState<string>(state.store?.retailerId ?? "");
  const selectedRetailer = useMemo(
    () => retailersQuery.data?.find((retailer) => retailer.id === retailerId),
    [retailersQuery.data, retailerId],
  );

  // Read-only context: how many of the chain's stores we aggregate prices from. The specific
  // store is irrelevant to pricing ("la tienda da igual"); this is purely informational.
  const storesQuery = useStoresQuery(retailerId || undefined);
  const storeCount = storesQuery.data?.length ?? null;

  const retailerOptions = (retailersQuery.data ?? []).map((retailer) => {
    const base = retailer.is_synthetic ? `${retailer.name} (datos sintéticos)` : retailer.name;
    return {
      value: retailer.id,
      // Flag chains we can't cost so the choice is informed before generating.
      label: retailer.costing_supported ? base : `${base} — solo visor de precios`,
    };
  });

  const onContinue = () => {
    setStore({
      retailerId: retailerId || null,
      retailerLabel: selectedRetailer?.name ?? null,
    });
    router.push("/onboarding/miembros");
  };

  return (
    <div className="flex flex-col gap-6">
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

        {selectedRetailer && !selectedRetailer.costing_supported ? (
          <Alert tone="warning" title="Solo visor de precios reales">
            Esta cadena tiene precios reales pero escasos y sin contenido por envase, así
            que los planes saldrán con cobertura baja y coste poco fiable. Para planes
            costeados al 100 %, elige <strong>MercaEjemplo</strong> o un catálogo importado.
          </Alert>
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

      <Card>
        <CardHeader>
          <CardTitle>Estado de las cadenas</CardTitle>
          <CardDescription>
            Las siete cadenas iniciales y su estado de integración. Solo las marcadas como
            <strong> Disponible</strong> permiten costear un plan completo; el resto están en
            pruebas, muestran solo ofertas o aún no tienen datos.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {providersQuery.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : providersQuery.isError ? (
            <Alert tone="warning" title="Estado de proveedores no disponible">
              No hemos podido cargar el estado de las cadenas. Puedes continuar igualmente.
            </Alert>
          ) : (providersQuery.data ?? []).length === 0 ? (
            <Alert tone="info">Todavía no hay proveedores dados de alta.</Alert>
          ) : (
            <ul className="divide-y divide-border">
              {(providersQuery.data ?? [])
                .filter((provider) => provider.intended_catalog_scope !== "complementary")
                .map((provider) => (
                  <ChainStatusRow key={provider.provider} provider={provider} />
                ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
