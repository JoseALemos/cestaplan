"use client";

import { useState } from "react";

import { formatMoney, formatQuantity } from "@/lib/utils/format";
import type { GroceryItem } from "@/lib/api/types";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/utils/cn";

export interface GroceryItemRowProps {
  item: GroceryItem;
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

export function GroceryItemRow({
  item,
  currency,
  checked,
  onToggle,
  onSubstitute,
  substituting,
}: GroceryItemRowProps) {
  const [substituteOpen, setSubstituteOpen] = useState(false);
  const [productId, setProductId] = useState("");

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
              {formatMoney(item.subtotal, currency)}{" "}
              <Badge tone={item.subtotal_known ? "success" : "warning"} className="align-middle">
                {item.subtotal_known ? "conocido" : "estimado"}
              </Badge>
            </p>
          </div>

          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
            <span>Necesario: {formatQuantity(item.needed_quantity, undefined)}</span>
            {item.packages_count != null ? (
              <span>
                {item.packages_count} envase(s) de {formatQuantity(item.package_quantity, item.package_unit ?? undefined)}
              </span>
            ) : null}
            {item.unit_price ? <span>{formatMoney(item.unit_price, currency)}/envase</span> : null}
            {pantryBadgeLabel(item.pantry_available) ? (
              <Badge tone="info">{pantryBadgeLabel(item.pantry_available)}</Badge>
            ) : null}
            {item.availability ? <span>{item.availability}</span> : null}
          </div>

          {item.source ? (
            <p className="mt-1 text-xs text-ink-faint">
              {item.source.source_name}
              {item.source.observed_at ? ` · ${new Date(item.source.observed_at).toLocaleDateString("es-ES")}` : ""}
            </p>
          ) : null}

          <div className="mt-2">
            {substituteOpen ? (
              <form
                className="flex items-end gap-2"
                onSubmit={async (event) => {
                  event.preventDefault();
                  if (!productId.trim()) return;
                  await onSubstitute(productId.trim());
                  setProductId("");
                  setSubstituteOpen(false);
                }}
              >
                <Input
                  label="ID del producto sustituto"
                  value={productId}
                  onChange={(event) => setProductId(event.target.value)}
                  className="w-56"
                />
                <Button type="submit" size="sm" loading={substituting}>
                  Sustituir
                </Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setSubstituteOpen(false)}>
                  Cancelar
                </Button>
              </form>
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
