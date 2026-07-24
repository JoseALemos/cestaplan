import type { GroceryList } from "@/lib/api/types";
import { formatCategoryLabel } from "@/lib/utils/shopping-format";

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
