"use client";

import { useParams, useSearchParams } from "next/navigation";
import { useMemo } from "react";

import { addGroceryItem, substituteGroceryItem } from "@/lib/api/endpoints";
import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { useGroceryListQuery } from "@/lib/query/hooks/use-grocery";
import { useGroceryChecklistSync } from "@/lib/offline/use-grocery-checklist";
import { useOnlineStatus } from "@/lib/offline/use-online-status";
import { exportGroceryListAsCsv, exportGroceryListAsJson } from "@/lib/utils/export";
import { formatMoney } from "@/lib/utils/format";
import { queryKeys } from "@/lib/query/keys";
import { useQueryClient } from "@tanstack/react-query";

import { AddItemForm } from "@/components/grocery/AddItemForm";
import { GroceryItemRow } from "@/components/grocery/GroceryItemRow";
import { OfflineIndicator } from "@/components/grocery/OfflineIndicator";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";

export default function GroceryListPage() {
  const params = useParams<{ mealPlanId: string }>();
  const searchParams = useSearchParams();
  const mealPlanId = params.mealPlanId;
  const [currentHouseholdId] = useCurrentHouseholdId();
  const householdId = searchParams.get("householdId") ?? currentHouseholdId ?? "";
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const listQuery = useGroceryListQuery(mealPlanId);
  const isOnline = useOnlineStatus();

  const invalidateList = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.groceryList(mealPlanId) });

  const checklist = useGroceryChecklistSync(mealPlanId, invalidateList);

  const allItems = useMemo(
    () => listQuery.data?.categories.flatMap((category) => category.items) ?? [],
    [listQuery.data],
  );
  const checkedCount = allItems.filter((item) => checklist.effectiveChecked(item.id, item.is_checked)).length;

  if (listQuery.isLoading) {
    return (
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (listQuery.isError || !listQuery.data) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <Alert tone="error">
          No se pudo cargar la lista de la compra{isOnline ? "" : " (estás sin conexión)"}.
        </Alert>
      </div>
    );
  }

  const list = listQuery.data;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6 print:px-0">
      <Card className="print:hidden">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle>Lista de la compra</CardTitle>
            <OfflineIndicator isOnline={checklist.isOnline} pendingCount={checklist.pendingCount} />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <ProgressBar
            value={checkedCount}
            max={allItems.length || 1}
            label={`${checkedCount} de ${allItems.length} comprados`}
          />
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-ink-muted">Coste conocido</p>
              <p className="font-display text-display-sm text-ink">
                {formatMoney(list.known_cost, list.currency)}
              </p>
            </div>
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-ink-muted">Coste estimado adicional</p>
              <p className="font-display text-display-sm text-ink">
                {formatMoney(list.estimated_cost, list.currency)}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => window.print()}>
              Imprimir
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => exportGroceryListAsCsv(list)}>
              Exportar CSV
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => exportGroceryListAsJson(list)}>
              Exportar JSON
            </Button>
          </div>
          <AddItemForm
            disabled={!isOnline || !householdId}
            disabledReason={
              !householdId
                ? "Abre la lista desde tu plan para poder añadir productos."
                : "Necesitas conexión para añadir un producto nuevo."
            }
            onAdd={async (input) => {
              try {
                await addGroceryItem(mealPlanId, input);
                await invalidateList();
                showToast({ tone: "success", title: "Producto añadido" });
              } catch {
                showToast({ tone: "error", title: "No se pudo añadir el producto" });
              }
            }}
          />
        </CardContent>
      </Card>

      {list.categories.length === 0 ? (
        <Alert tone="info">Esta lista todavía no tiene productos.</Alert>
      ) : (
        list.categories.map((category) => (
          <section key={category.category} className="flex flex-col gap-3">
            <h2 className="font-display text-display-sm text-ink">{category.category}</h2>
            <ul className="flex flex-col gap-2">
              {category.items.map((item) => (
                <GroceryItemRow
                  key={item.id}
                  item={item}
                  currency={list.currency}
                  checked={checklist.effectiveChecked(item.id, item.is_checked)}
                  onToggle={() => void checklist.toggle(item.id, item.is_checked)}
                  substituting={false}
                  onSubstitute={async (productId) => {
                    try {
                      await substituteGroceryItem(mealPlanId, item.id, { product_id: productId });
                      await invalidateList();
                      showToast({ tone: "success", title: "Producto sustituido" });
                    } catch {
                      showToast({ tone: "error", title: "No se pudo sustituir el producto" });
                    }
                  }}
                />
              ))}
            </ul>
          </section>
        ))
      )}

      <p className="text-xs text-ink-faint">
        Los precios llevan fuente y fecha cuando se conocen; los importes marcados como
        &ldquo;estimado&rdquo; no están confirmados por un dato de precio real.{" "}
        <Badge tone="neutral">{list.coverage_status}</Badge>
      </p>
    </div>
  );
}
