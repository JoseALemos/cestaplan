import type { ComparisonChain } from "@/lib/api/types";
import { coverageTone } from "@/lib/domain/labels";
import { formatMoney, formatQuantity } from "@/lib/utils/format";

import { Badge } from "@/components/ui/Badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";

export interface ChainCardProps {
  chain: ComparisonChain;
  currency: string;
  /** The cheapest chain that fully covers the basket — visually marked as the top pick. */
  highlighted?: boolean;
}

/** One retailer's total cost + price-coverage badge for the multi-chain comparison. */
export function ChainCard({ chain, currency, highlighted = false }: ChainCardProps) {
  const { with_price, without_price } = chain.coverage;
  const totalIngredients = with_price + without_price;

  return (
    <Card
      className={highlighted ? "border-2 border-success ring-1 ring-success/30" : undefined}
    >
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <CardTitle>{chain.retailer_name}</CardTitle>
          {highlighted ? <Badge tone="success">Más barata · cobertura completa</Badge> : null}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p className="font-display text-display-lg text-ink">{formatMoney(chain.known_cost, currency)}</p>
        <div>
          {chain.full_coverage ? (
            <Badge tone="success">Cobertura completa</Badge>
          ) : (
            <Badge tone={coverageTone(chain.coverage.status)}>
              Cobertura parcial {with_price}/{totalIngredients}
            </Badge>
          )}
        </div>
        {chain.travel?.travel_cost ? (
          <div className="mt-1 flex flex-col gap-1 border-t border-border pt-2">
            <p className="text-sm text-ink-muted">
              Desplazamiento: {formatMoney(chain.travel.travel_cost, currency)}
              {chain.travel.distance_km && chain.travel.nearest_store?.name
                ? ` (~${formatQuantity(chain.travel.distance_km, "km")} a ${chain.travel.nearest_store.name})`
                : null}
            </p>
            {chain.total_with_travel ? (
              <p className="font-semibold text-ink">
                Total con desplazamiento: {formatMoney(chain.total_with_travel, currency)}
              </p>
            ) : null}
          </div>
        ) : chain.travel && !chain.travel.found ? (
          <p className="mt-1 text-xs text-ink-faint">Sin tienda cercana localizada</p>
        ) : null}
      </CardContent>
    </Card>
  );
}
