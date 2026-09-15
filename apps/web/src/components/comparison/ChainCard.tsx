import type { ComparisonChain } from "@/lib/api/types";
import { coverageTone } from "@/lib/domain/labels";
import { formatMoney } from "@/lib/utils/format";

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
      </CardContent>
    </Card>
  );
}
