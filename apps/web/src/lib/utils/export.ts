import type { GroceryList } from "@/lib/api/types";
import { formatMoney } from "@/lib/utils/format";
import { formatCategoryLabel, formatRequiredQuantity } from "@/lib/utils/shopping-format";

function downloadBlob(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/**
 * Plain-text rendering of the list, for sharing (WhatsApp/Notes) or the clipboard.
 * Uses `*bold*` category headers (WhatsApp renders them) and ☑/☐ so a half-checked list
 * shared mid-shop still reads correctly. No prices per line — just what to buy — with the
 * total outlay at the foot. A non-breaking space keeps "500 g" from wrapping oddly.
 */
export function groceryListToText(list: GroceryList): string {
  const lines: string[] = ["🛒 Lista de la compra — CestaPlan", ""];
  for (const category of list.categories) {
    lines.push(`*${formatCategoryLabel(category.category)}*`);
    for (const item of category.items) {
      const mark = item.is_checked ? "☑" : "☐";
      const qty = formatRequiredQuantity(item.required_quantity, item.required_unit);
      lines.push(`${mark} ${item.generic_name} — ${qty}`.replace(/ /g, " "));
    }
    lines.push("");
  }
  lines.push(`Desembolso estimado: ${formatMoney(list.purchase_outlay, list.currency)}`);
  return lines.join("\n").trimEnd();
}

export function exportGroceryListAsJson(list: GroceryList): void {
  downloadBlob(
    JSON.stringify(list, null, 2),
    `lista-compra-${list.meal_plan_id}.json`,
    "application/json",
  );
}

function csvEscape(value: string | number | null | undefined): string {
  const text = value === null || value === undefined ? "" : String(value);
  if (/[",\n;]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

export function exportGroceryListAsCsv(list: GroceryList): void {
  const header = [
    "categoria",
    "producto_generico",
    "producto",
    "cantidad_necesaria",
    "unidad",
    "envases",
    "precio_envase",
    "precio_normalizado",
    "unidad_normalizada",
    "desembolso",
    "coste_consumido",
    "valor_sobrante",
    "fuente",
    "comprado",
  ];
  const rows = list.categories.flatMap((category) =>
    category.items.map((item) =>
      [
        formatCategoryLabel(category.category),
        item.generic_name,
        item.product_name ?? "",
        item.required_quantity,
        item.required_unit ?? "",
        item.packages_required ?? "",
        item.package_price ?? "",
        item.normalized_unit_price ?? "",
        item.normalized_unit ?? "",
        item.purchased_cost ?? "",
        item.consumed_cost ?? "",
        item.leftover_value ?? "",
        item.price_source_kind,
        item.is_checked ? "si" : "no",
      ]
        .map(csvEscape)
        .join(","),
    ),
  );
  downloadBlob([header.join(","), ...rows].join("\n"), `lista-compra-${list.meal_plan_id}.csv`, "text/csv");
}
