"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";

import { usePlanComparisonQuery } from "@/lib/query/hooks/use-plans";
import { formatMoney, formatQuantity } from "@/lib/utils/format";
import type { ComparisonChain } from "@/lib/api/types";

import { ChainCard } from "@/components/comparison/ChainCard";
import { Alert } from "@/components/ui/Alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";

/** Cheapest first — matches how the per-chain cards and the "cheapest full coverage" mark read. */
function sortByKnownCost(chains: ComparisonChain[]): ComparisonChain[] {
  return [...chains].sort(
    (a, b) => Number.parseFloat(a.known_cost) - Number.parseFloat(b.known_cost),
  );
}

export default function PlanComparisonPage() {
  const params = useParams<{ mealPlanId: string }>();
  const searchParams = useSearchParams();
  const mealPlanId = params.mealPlanId;
  const householdId = searchParams.get("householdId") ?? "";

  const comparisonQuery = usePlanComparisonQuery(mealPlanId);

  if (comparisonQuery.isLoading) {
    return (
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (comparisonQuery.isError || !comparisonQuery.data) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <Alert tone="error">
          No se pudo cargar la comparativa de precios. Comprueba tu conexión e inténtalo de nuevo.
        </Alert>
      </div>
    );
  }

  const comparison = comparisonQuery.data;
  const currency = comparison.currency;
  const chains = sortByKnownCost(comparison.chains);
  // Cheapest chain (first in ascending order) among the ones with full coverage.
  const cheapestFullCoverageId = chains.find((chain) => chain.full_coverage)?.retailer_id;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <Link
          href={`/planes/${mealPlanId}${householdId ? `?householdId=${householdId}` : ""}`}
          className="text-sm font-medium text-primary hover:underline"
        >
          ← Volver al plan
        </Link>
        <h1 className="mt-2 font-display text-display-md text-ink">
          Comparar precios entre supermercados
        </h1>
        <p className="mt-1 text-sm text-ink-muted">
          Cesta de {comparison.basket.ingredient_count} ingrediente(s), comparada en {chains.length}{" "}
          cadena(s).
        </p>
      </div>

      {comparison.best_single ? (
        <Alert tone="success" title="Mejor comprando todo en una tienda">
          {comparison.best_single.retailer_name} —{" "}
          {formatMoney(comparison.best_single.total, currency)}
        </Alert>
      ) : (
        <Alert tone="warning" title="Ninguna cadena cubre toda la cesta">
          Ninguna cadena tiene precio para todos los ingredientes del plan. Consulta el reparto
          óptimo entre varias tiendas para minimizar el coste.
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Reparto óptimo entre tiendas</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <p className="text-[0.95rem] text-ink">
            {formatMoney(comparison.split.total, currency)} en {comparison.split.distinct_chain_count}{" "}
            tienda(s)
            {comparison.split.savings_vs_best_single !== null
              ? ` — ahorras ${formatMoney(
                  comparison.split.savings_vs_best_single,
                  currency,
                )} frente a comprar todo en una sola tienda.`
              : "."}
          </p>

          {comparison.split.by_chain.length > 0 ? (
            <div className="flex flex-col gap-3">
              {comparison.split.by_chain.map((chainSplit) => (
                <div key={chainSplit.retailer_id} className="rounded-md border border-border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-medium text-ink">{chainSplit.retailer_name}</p>
                    <p className="font-display text-display-sm text-ink">
                      {formatMoney(chainSplit.subtotal, currency)}
                    </p>
                  </div>
                  <ul className="mt-2 flex flex-col gap-1 text-sm text-ink-muted">
                    {chainSplit.items.map((item) => (
                      <li key={item.canonical_name} className="flex items-center justify-between gap-2">
                        <span>
                          {item.display_name} · {formatQuantity(item.required_quantity, item.required_unit)}
                        </span>
                        <span className="text-ink">{formatMoney(item.cost, currency)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          ) : null}

          {comparison.split.uncovered_ingredients.length > 0 ? (
            <Alert tone="warning" title="Sin precio en ninguna cadena">
              <ul className="flex flex-col gap-1">
                {comparison.split.uncovered_ingredients.map((ingredient) => (
                  <li key={ingredient.canonical_name}>
                    {ingredient.display_name} ·{" "}
                    {formatQuantity(ingredient.required_quantity, ingredient.required_unit)}
                  </li>
                ))}
              </ul>
            </Alert>
          ) : null}
        </CardContent>
      </Card>

      <section className="flex flex-col gap-3">
        <h2 className="font-display text-display-sm text-ink">Coste por cadena</h2>
        {chains.length === 0 ? (
          <Alert tone="info">No hay cadenas con catálogo disponible para comparar todavía.</Alert>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {chains.map((chain) => (
              <ChainCard
                key={chain.retailer_id}
                chain={chain}
                currency={currency}
                highlighted={chain.retailer_id === cheapestFullCoverageId}
              />
            ))}
          </div>
        )}
      </section>

      <p className="text-xs text-ink-faint">
        Los costes son los de la cesta de compra (paquetes completos, con la despensa vacía): un
        formato de envase más grande puede salir más caro aquí aunque su precio por unidad sea
        menor.
      </p>
    </div>
  );
}
