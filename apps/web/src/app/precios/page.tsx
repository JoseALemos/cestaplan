"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { retailerSelectState } from "@/lib/domain/retailer-select-state";
import {
  useRetailersQuery,
  useStorePricesQuery,
  useStoresQuery,
} from "@/lib/query/hooks/use-catalog";
import { formatDate, formatMoney } from "@/lib/utils/format";
import type { StorePriceItem } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";

const PAGE_SIZE = 20;

function PriceRow({ item }: { item: StorePriceItem }) {
  return (
    <li className="flex flex-col gap-2.5 rounded-md border border-border px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-ink">{item.product_name}</p>
        <p className="mt-0.5 truncate text-xs text-ink-faint">
          {[item.brand, item.barcode].filter(Boolean).join(" · ") || "Sin marca registrada"}
        </p>
      </div>

      <div className="flex items-center justify-between gap-4 sm:flex-col sm:items-end sm:justify-start sm:gap-1">
        <div className="flex items-baseline gap-1.5">
          <span className="text-base font-semibold text-ink">
            {formatMoney(item.amount, item.currency)}
          </span>
          {item.unit_price ? (
            <span className="text-xs text-ink-faint">
              ({formatMoney(item.unit_price, item.currency)}/{item.package_unit ?? "ud"})
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-ink-faint">{formatDate(item.observed_at)}</span>
          {item.source_url ? (
            <a href={item.source_url} target="_blank" rel="noreferrer">
              <Badge tone="info" className="hover:brightness-95">
                Open Prices ↗
              </Badge>
            </a>
          ) : (
            <Badge tone="neutral">{item.source_name}</Badge>
          )}
        </div>
      </div>
    </li>
  );
}

export default function PreciosPage() {
  const router = useRouter();
  const { isAuthenticated, isLoading: authLoading } = useAuth();

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [authLoading, isAuthenticated, router]);

  const retailersQuery = useRetailersQuery();
  const [retailerId, setRetailerId] = useState("");
  const storesQuery = useStoresQuery(retailerId || undefined);

  const [storeId, setStoreId] = useState("");
  const selectedStore = useMemo(
    () => storesQuery.data?.find((store) => store.id === storeId),
    [storesQuery.data, storeId],
  );

  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);

  // Debounce the free-text search so we don't fire a request per keystroke.
  useEffect(() => {
    const handle = setTimeout(() => {
      setSearch(searchInput.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const pricesQuery = useStorePricesQuery(retailerId || undefined, storeId || undefined, {
    search,
    page,
    size: PAGE_SIZE,
  });

  const retailerOptions = (retailersQuery.data ?? []).map((retailer) => ({
    value: retailer.id,
    label: retailer.is_synthetic ? `${retailer.name} (datos sintéticos, sin precios reales)` : retailer.name,
  }));
  const chainState = retailerSelectState({
    isSuccess: retailersQuery.isSuccess,
    isError: retailersQuery.isError,
    optionCount: retailerOptions.length,
  });
  const storeOptions = (storesQuery.data ?? []).map((store) => ({
    value: store.id,
    label: `${store.name} — ${store.locality} (${store.postal_code}) · ${store.priced_product_count} producto${store.priced_product_count === 1 ? "" : "s"}`,
  }));

  const totalPages = pricesQuery.data ? Math.max(1, Math.ceil(pricesQuery.data.count / PAGE_SIZE)) : 1;

  if (authLoading || !isAuthenticated) {
    return null;
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Precios reales</h1>
        <p className="mt-2 text-ink-muted">
          Explora precios reales observados por la comunidad, tienda a tienda, a través de
          Open Food Facts — Open Prices.
        </p>
      </div>

      <Alert tone="info" title="Esto es un visor, no el planificador">
        Estos precios reales son orientativos y no se usan para generar planes; el
        planificador usa el catálogo de demostración o tus importaciones.
      </Alert>

      <Card>
        <CardHeader>
          <CardTitle>Elige cadena y tienda</CardTitle>
          <CardDescription>
            Solo se listan cadenas y tiendas que ya tienen al menos un precio real.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {chainState === "loading" ? (
            <Skeleton className="h-11 w-full" />
          ) : chainState === "error" ? (
            <Alert tone="warning">No se pudo cargar la lista de cadenas. Comprueba tu conexión.</Alert>
          ) : chainState === "empty" ? (
            <Alert tone="info">Todavía no hay cadenas con precios reales sincronizados.</Alert>
          ) : (
            <Select
              label="Cadena"
              placeholder="Selecciona una cadena"
              options={retailerOptions}
              value={retailerId}
              onChange={(event) => {
                setRetailerId(event.target.value);
                setStoreId("");
                setPage(1);
              }}
            />
          )}

          {retailerId ? (
            storesQuery.isLoading ? (
              <Skeleton className="h-11 w-full" />
            ) : storesQuery.isError ? (
              <Alert tone="warning">No se pudieron cargar las tiendas de esta cadena.</Alert>
            ) : storeOptions.length === 0 ? (
              <Alert tone="info">Esta cadena todavía no tiene tiendas con precios reales.</Alert>
            ) : (
              <Select
                label="Tienda"
                placeholder="Selecciona una tienda"
                options={storeOptions}
                value={storeId}
                onChange={(event) => {
                  setStoreId(event.target.value);
                  setPage(1);
                }}
              />
            )
          ) : null}
        </CardContent>
      </Card>

      {storeId ? (
        <Card>
          <CardHeader>
            <CardTitle>{selectedStore?.name ?? "Tienda"}</CardTitle>
            <CardDescription>
              {selectedStore ? `${selectedStore.locality} (${selectedStore.postal_code})` : null}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <Input
              label="Buscar producto"
              placeholder="p. ej. leche, tomate frito…"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
            />

            {pricesQuery.isLoading ? (
              <div className="flex flex-col gap-2">
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
              </div>
            ) : pricesQuery.isError ? (
              <Alert tone="error">
                No se pudieron cargar los precios de esta tienda. Inténtalo de nuevo.
              </Alert>
            ) : (pricesQuery.data?.items.length ?? 0) === 0 ? (
              <Alert tone="info">
                {search
                  ? "Ningún producto de esta tienda coincide con la búsqueda."
                  : "Aún no hay precios reales para esta tienda."}
              </Alert>
            ) : (
              <>
                <ul className="flex flex-col gap-2">
                  {pricesQuery.data?.items.map((item) => (
                    <PriceRow key={item.product_id} item={item} />
                  ))}
                </ul>

                <div className="flex items-center justify-between gap-3 pt-1">
                  <p className="text-xs text-ink-faint">
                    {pricesQuery.data?.count} producto{pricesQuery.data?.count === 1 ? "" : "s"} con precio real
                  </p>
                  {totalPages > 1 ? (
                    <div className="flex items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={page <= 1}
                        onClick={() => setPage((current) => Math.max(1, current - 1))}
                      >
                        Anterior
                      </Button>
                      <span className="text-xs text-ink-muted">
                        Página {page} de {totalPages}
                      </span>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={page >= totalPages}
                        onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                      >
                        Siguiente
                      </Button>
                    </div>
                  ) : null}
                </div>
              </>
            )}
          </CardContent>
        </Card>
      ) : null}

      {pricesQuery.data?.attribution ? (
        <div className="rounded-md border border-border bg-bg-subtle px-4 py-3 text-xs text-ink-faint">
          <p>
            {pricesQuery.data.attribution}
            {pricesQuery.data.license_code ? ` (licencia ${pricesQuery.data.license_code}).` : ""}
          </p>
        </div>
      ) : null}
    </div>
  );
}
