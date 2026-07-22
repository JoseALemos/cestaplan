import type { GroceryList } from "@/lib/api/types";

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
    "envases",
    "precio_envase",
    "subtotal",
    "coste_conocido",
    "comprado",
  ];
  const rows = list.categories.flatMap((category) =>
    category.items.map((item) =>
      [
        category.category,
        item.generic_name,
        item.product_name ?? "",
        item.needed_quantity,
        item.packages_count ?? "",
        item.unit_price ?? "",
        item.subtotal ?? "",
        item.subtotal_known ? "si" : "no",
        item.is_checked ? "si" : "no",
      ]
        .map(csvEscape)
        .join(","),
    ),
  );
  downloadBlob([header.join(","), ...rows].join("\n"), `lista-compra-${list.meal_plan_id}.csv`, "text/csv");
}
