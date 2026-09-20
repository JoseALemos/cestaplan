import type { PlanPersonalization } from "@/lib/api/types";
import { ALLERGEN_OPTIONS, DIET_TYPE_OPTIONS } from "@/lib/domain/labels";

import { Badge } from "@/components/ui/Badge";

const DIET_LABELS: Record<string, string> = Object.fromEntries(
  DIET_TYPE_OPTIONS.filter((o) => o.value).map((o) => [o.value, o.label]),
);
const ALLERGEN_LABELS: Record<string, string> = Object.fromEntries(
  ALLERGEN_OPTIONS.map((o) => [o.code, o.label]),
);

const listLabels = (codes: string[], map: Record<string, string>): string =>
  codes.map((c) => map[c] ?? c).join(", ");

/** Makes visible what the planner already did for the household (favourites, rejections,
 *  diet, allergens). When there is no signal yet, it invites the user to start teaching it. */
export function PersonalizationSummary({
  personalization,
}: {
  personalization: PlanPersonalization;
}) {
  const { favorites_included, rejected_hidden, diet_labels, allergens_avoided } =
    personalization;

  const chips: { key: string; tone: "success" | "info" | "neutral"; text: string }[] = [];
  if (favorites_included > 0) {
    chips.push({
      key: "fav",
      tone: "success",
      text:
        favorites_included === 1
          ? "Incluye 1 de tus favoritas"
          : `Incluye ${favorites_included} de tus favoritas`,
    });
  }
  if (rejected_hidden > 0) {
    chips.push({
      key: "rej",
      tone: "neutral",
      text:
        rejected_hidden === 1
          ? "Oculté 1 receta que descartaste"
          : `Oculté ${rejected_hidden} recetas que descartaste`,
    });
  }
  if (diet_labels.length > 0) {
    chips.push({ key: "diet", tone: "info", text: `Respeta: ${listLabels(diet_labels, DIET_LABELS)}` });
  }
  if (allergens_avoided.length > 0) {
    chips.push({
      key: "alg",
      tone: "info",
      text: `Sin ${listLabels(allergens_avoided, ALLERGEN_LABELS)}`,
    });
  }

  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <p className="text-sm font-medium text-ink">Personalizado para ti</p>
      {chips.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {chips.map((chip) => (
            <Badge key={chip.key} tone={chip.tone}>
              {chip.text}
            </Badge>
          ))}
        </div>
      ) : (
        <p className="mt-1 text-xs text-ink-muted">
          Marca ♥ las recetas que te gusten y descarta las que no: los próximos planes se
          irán adaptando a tu hogar.
        </p>
      )}
    </div>
  );
}
