/**
 * Pure, display-only formatters for the shopping list. No React, no I/O — unit-tested directly.
 *
 * Money/quantity values arrive from the API as strings and stay strings in state; these helpers
 * parse to number only to render. A package price is the whole-package price ("€/envase"); a
 * normalized unit price is a readable reference (€/kg, €/l, €/unidad) — never a per-gram value
 * rounded to cents.
 */

const NBSP = " ";

function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isNaN(n) ? null : n;
}

function money(value: number, currency: string): string {
  return new Intl.NumberFormat("es-ES", { style: "currency", currency }).format(value);
}

function decimal(value: number, maximumFractionDigits = 2): string {
  return new Intl.NumberFormat("es-ES", { maximumFractionDigits }).format(value);
}

/** Human unit labels: `ud` for discrete units; readable mass/volume otherwise. */
function unitLabel(unit: string | null | undefined): string {
  const u = (unit ?? "").toLowerCase();
  if (u === "unit" || u === "ud") return "ud";
  return u;
}

/**
 * A required/purchased/leftover quantity WITH its unit, normalized for readability:
 * g→kg and ml→l above 1000, `ud` for discrete units. Never loses precision (only display).
 */
export function formatRequiredQuantity(
  value: string | number | null | undefined,
  unit: string | null | undefined,
): string {
  const n = toNumber(value);
  if (n === null) return "—";
  const u = (unit ?? "").toLowerCase();
  if ((u === "g" || u === "kg") && Math.abs(u === "kg" ? n * 1000 : n) >= 1000) {
    const kg = u === "kg" ? n : n / 1000;
    return `${decimal(kg, 3)}${NBSP}kg`;
  }
  if ((u === "ml" || u === "l") && Math.abs(u === "l" ? n * 1000 : n) >= 1000) {
    const l = u === "l" ? n : n / 1000;
    return `${decimal(l, 3)}${NBSP}l`;
  }
  const label = unitLabel(unit);
  return label ? `${decimal(n)}${NBSP}${label}` : decimal(n);
}

/** The whole-package price: e.g. "3,19 €/envase". "—" when unknown. */
export function formatPackagePrice(
  value: string | number | null | undefined,
  currency = "EUR",
): string {
  const n = toNumber(value);
  if (n === null) return "—";
  return `${money(n, currency)}/envase`;
}

/**
 * The line's purchase outlay, distinguishing single vs multiple packages:
 * "1,62 €" for one package, "2 envases · 1,62 €" for several.
 */
export function formatPurchaseLine(
  purchasedCost: string | number | null | undefined,
  packagesRequired: number | null | undefined,
  currency = "EUR",
): string {
  const n = toNumber(purchasedCost);
  if (n === null) return "—";
  const packages = packagesRequired ?? 0;
  if (packages > 1) return `${packages} envases · ${money(n, currency)}`;
  return money(n, currency);
}

/** A readable reference price: e.g. "6,38 €/l", "5,94 €/kg", "0,17 €/unidad". */
export function formatNormalizedUnitPrice(
  value: string | number | null | undefined,
  unit: string | null | undefined,
  currency = "EUR",
): string {
  const n = toNumber(value);
  if (n === null || !unit) return "—";
  return `${money(n, currency)}/${unit}`;
}

const SOURCE_KIND_LABEL: Record<string, string> = {
  demo: "Precio demo",
  confirmed_external: "Precio confirmado",
  estimated: "Precio estimado",
  unavailable: "Sin precio",
};

/** Accessible, text-first source label: kind · source name · date. Never color-only. */
export function formatSourceLabel(
  kind: string | null | undefined,
  sourceName: string | null | undefined,
  observedAt: string | null | undefined,
): string {
  const parts: string[] = [SOURCE_KIND_LABEL[kind ?? ""] ?? "Sin precio"];
  if (sourceName) parts.push(sourceName);
  if (observedAt) {
    const date = new Date(observedAt);
    if (!Number.isNaN(date.getTime())) {
      parts.push(new Intl.DateTimeFormat("es-ES", { dateStyle: "short" }).format(date));
    }
  }
  return parts.join(" · ");
}

/** Centralized ingredient-category slug → human label map (Spanish). */
export const CATEGORY_LABELS: Record<string, string> = {
  aceites_condimentos: "Aceites y condimentos",
  carne: "Carne",
  cereales_pasta_arroz: "Cereales, pasta y arroz",
  conservas_despensa: "Conservas y despensa",
  frutas: "Frutas",
  frutos_secos_semillas: "Frutos secos y semillas",
  huevos: "Huevos",
  lacteos: "Lácteos",
  legumbres: "Legumbres",
  panaderia: "Panadería",
  pescado_marisco: "Pescado y marisco",
  verduras: "Verduras",
  uncategorized: "Sin categoría",
};

/** Human category label for a slug; falls back to a de-slugged title (never a raw slug heading). */
export function formatCategoryLabel(slug: string | null | undefined): string {
  const key = (slug ?? "").toLowerCase();
  if (CATEGORY_LABELS[key]) return CATEGORY_LABELS[key];
  if (!key) return "Sin categoría";
  return key
    .split("_")
    .map((w) => (w ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(" ");
}
