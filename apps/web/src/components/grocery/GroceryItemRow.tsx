"use client";

import { useEffect, useState } from "react";

import { formatMoney, formatQuantity } from "@/lib/utils/format";
import {
  formatNormalizedUnitPrice,
  formatPackagePrice,
  formatPurchaseLine,
  formatRequiredQuantity,
  formatSourceLabel,
} from "@/lib/utils/shopping-format";
import { useProductSearchQuery } from "@/lib/query/hooks/use-grocery";
import type { GroceryItem, PriceSourceKind } from "@/lib/api/types";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/utils/cn";

export interface GroceryItemRowProps {
  item: GroceryItem;
  mealPlanId: string;
  currency: string;
  checked: boolean;
  onToggle: () => void;
  onSubstitute: (productId: string) => Promise<void>;
  substituting: boolean;
}

/** `pantry_available` is observed as a plain boolean, but typed loosely — handle a quantity string too. */
function pantryBadgeLabel(pantryAvailable: GroceryItem["pantry_available"]): string | null {
  if (pantryAvailable === true) return "Ya tienes en casa";
  if (typeof pantryAvailable === "string") {
    const quantity = Number.parseFloat(pantryAvailable);
    if (!Number.isNaN(quantity) && quantity > 0) return `Ya tienes ${formatQuantity(pantryAvailable)}`;
  }
  return null;
}

const SOURCE_KIND_TONE: Record<PriceSourceKind, "info" | "success" | "warning" | "error"> = {
  demo: "info",
  confirmed_external: "success",
  estimated: "warning",
  unavailable: "error",
};
const SOURCE_KIND_SHORT: Record<PriceSourceKind, string> = {
  demo: "demo",
  confirmed_external: "confirmado",
  estimated: "estimado",
  unavailable: "sin precio",
};

export function GroceryItemRow({
  item,
  mealPlanId,
  currency,
  checked,
  onToggle,
  onSubstitute,
  substituting,
}: GroceryItemRowProps) {
  const [substituteOpen, setSubstituteOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");

  // Debounce the query so we don't hit the catalogue on every keystroke.
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(search.trim()), 300);
    return () => clearTimeout(handle);
  }, [search]);

  const searchQuery = useProductSearchQuery(mealPlanId, debounced, substituteOpen);
  const results = searchQuery.data?.items ?? [];

  const closeSubstitute = () => {
    setSubstituteOpen(false);
    setSearch("");
    setDebounced("");
  };

  return (
    <li className={cn("rounded-md border border-border p-4", checked && "bg-bg-subtle")}>
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          aria-label={`Marcar ${item.generic_name} como comprado`}
          className="mt-1 h-5 w-5 shrink-0 accent-primary"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <p className={cn("text-sm font-medium text-ink", checked && "text-ink-faint line-through")}>
              {item.generic_name}
              {item.product_name ? (
                <span className="ml-1.5 font-normal text-ink-muted">· {item.product_name}</span>
              ) : null}
            </p>
            <p className="text-sm font-semibold text-ink">
              {formatPurchaseLine(item.purchased_cost, item.packages_required, currency)}{" "}
              <Badge tone={SOURCE_KIND_TONE[item.price_source_kind]} className="align-middle">
                {SOURCE_KIND_SHORT[item.price_source_kind]}
              </Badge>
            </p>
          </div>

          {/* Price detail: whole-package price + a readable reference price (never a per-gram value). */}
          {item.package_price ? (
            <p className="mt-0.5 text-xs text-ink-muted">
              {formatPackagePrice(item.package_price, currency)}
              {item.normalized_unit_price && item.normalized_unit
                ? ` · ${formatNormalizedUnitPrice(item.normalized_unit_price, item.normalized_unit, currency)}`
                : ""}
            </p>
          ) : null}

          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
            <span>Necesario: {formatRequiredQuantity(item.required_quantity, item.required_unit)}</span>
            {item.packages_required != null ? (
              <span>
                {item.packages_required} envase(s) de{" "}
                {formatRequiredQuantity(item.package_quantity, item.package_unit)}
              </span>
            ) : null}
            {item.purchased_quantity ? (
              <span>Comprado: {formatRequiredQuantity(item.purchased_quantity, item.package_unit)}</span>
            ) : null}
            {item.leftover_quantity && Number.parseFloat(item.leftover_quantity) > 0 ? (
              <span>Sobrante: {formatRequiredQuantity(item.leftover_quantity, item.package_unit)}</span>
            ) : null}
            {pantryBadgeLabel(item.pantry_available) ? (
              <Badge tone="info">{pantryBadgeLabel(item.pantry_available)}</Badge>
            ) : null}
            {item.availability ? <span>{item.availability}</span> : null}
          </div>

          <p className="mt-1 text-xs text-ink-faint">
            {formatSourceLabel(
              item.price_source_kind,
              item.source?.source_name,
              item.source?.observed_at,
            )}
          </p>

          <div className="mt-2">
            {substituteOpen ? (
              <div className="flex flex-col gap-2">
                <Input
                  label="Buscar producto sustituto"
                  placeholder="p. ej. tomate frito, garbanzos…"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  autoFocus
                />
                {searchQuery.isError ? (
                  <p className="text-xs text-error">No se pudo buscar. Inténtalo de nuevo.</p>
                ) : searchQuery.isFetching ? (
                  <p className="text-xs text-ink-faint">Buscando…</p>
                ) : debounced.length >= 2 && results.length === 0 ? (
                  <p className="text-xs text-ink-muted">Sin resultados para «{debounced}».</p>
                ) : debounced.length < 2 ? (
                  <p className="text-xs text-ink-faint">Escribe al menos 2 letras.</p>
                ) : null}

                {results.length > 0 ? (
                  <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto">
                    {results.map((product) => (
                      <li key={product.product_id}>
                        <button
                          type="button"
                          disabled={substituting}
                          onClick={async () => {
                            await onSubstitute(product.product_id);
                            closeSubstitute();
                          }}
                          className="flex w-full items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-left transition-colors hover:border-primary hover:bg-bg-subtle disabled:opacity-50"
                        >
                          <span className="min-w-0">
                            <span className="block truncate text-sm text-ink">{product.product_name}</span>
                            {product.brand ? (
                              <span className="block truncate text-xs text-ink-faint">{product.brand}</span>
                            ) : null}
                          </span>
                          {product.amount ? (
                            <span className="shrink-0 text-sm text-ink-muted">
                              {formatMoney(product.amount, product.currency)}
                            </span>
                          ) : null}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}

                <div>
                  <Button type="button" size="sm" variant="ghost" onClick={closeSubstitute}>
                    Cancelar
                  </Button>
                </div>
              </div>
            ) : (
              <Button type="button" size="sm" variant="ghost" onClick={() => setSubstituteOpen(true)}>
                Sustituir producto
              </Button>
            )}
          </div>
        </div>
      </div>
    </li>
  );
}
