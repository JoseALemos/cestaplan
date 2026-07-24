import type { InfeasibilityDiagnosis, ActionCode, PreflightCode } from "@/lib/api/types";

/** Presentation-ready view of a failed run's infeasibility diagnosis. */
export interface InfeasibilityView {
  message: string;
  code: string | undefined;
  actions: { code: string; label: string }[];
  showBudgetAdjust: boolean;
  canRetry: boolean;
  retryHint: string | null;
  minimumBudget: string | null;
}

// This module is loaded directly by Node's native test runner (node --test),
// which — unlike webpack/tsc — cannot resolve extensionless sibling *value*
// imports and rejects the `.ts` extension the app tsconfig would need. So, like
// the other node-tested pure modules here (provider-rights, retailer-select-
// state), it is self-contained: the label/message copy is redeclared with the
// same compile-time-exhaustive `Record<Enum,string>` shape as labels.ts, and
// plan-infeasibility.test.ts asserts the copy stays identical to labels.ts.

const ACTION_CODE_LABELS: Record<ActionCode, string> = {
  add_recipes: "Añadir más recetas al catálogo.",
  relax_soft_preferences: "Reducir las preferencias opcionales.",
  change_store: "Seleccionar otra cadena con catálogo disponible.",
  reduce_meals: "Reducir el número de comidas solicitadas.",
  increase_budget: "Aumentar el presupuesto.",
  configure_provider: "Configurar y sincronizar una fuente de precios.",
  review_mappings: "Revisar los productos pendientes de asociar a ingredientes.",
};

function actionLabel(code: string): string {
  return ACTION_CODE_LABELS[code as ActionCode] ?? "Ajuste sugerido";
}

const INFEASIBILITY_MESSAGES: Record<PreflightCode, string> = {
  no_active_recipes: "No hay recetas activas disponibles para construir el plan.",
  no_compatible_recipes: "Ninguna receta es compatible con las restricciones del hogar.",
  no_retailer_selected: "Selecciona una cadena con catálogo para poder calcular precios.",
  retailer_without_catalog: "La cadena seleccionada todavía no tiene un catálogo cargado.",
  no_mapped_products: "Todavía no hay productos asociados a los ingredientes.",
  no_product_prices:
    "Todavía no hay precios disponibles para calcular un plan con presupuesto.",
  no_costable_recipes:
    "Hay recetas disponibles, pero ninguna tiene todos sus ingredientes y precios necesarios para calcular el coste.",
  insufficient_recipe_variety:
    "No hay suficiente variedad de recetas costeables para las comidas solicitadas.",
  genuine_budget_infeasibility:
    "El presupuesto actual es inferior al mínimo estimado para las comidas solicitadas.",
  hard_constraints_infeasible:
    "Las restricciones obligatorias no dejan ninguna combinación válida.",
  optimizer_error: "Se produjo un error al generar el plan. Puedes reintentar.",
};

function infeasibilityMessage(code: string | undefined, fallback?: string | null): string {
  if (code && code in INFEASIBILITY_MESSAGES) {
    return INFEASIBILITY_MESSAGES[code as PreflightCode];
  }
  return fallback ?? "No se pudo generar un plan viable con los datos actuales.";
}

// Only these two codes represent conditions a plain retry might resolve: a
// genuine budget shortfall (retry after bumping the budget) and a transient
// optimizer error. Every other code is a deterministic precondition (missing
// recipes, prices, mappings…) that a retry can never fix on its own.
const RETRYABLE_CODES = new Set(["genuine_budget_infeasibility", "optimizer_error"]);

const PRICE_HINT_CODES = new Set(["no_product_prices", "retailer_without_catalog"]);
const MAPPING_HINT_CODES = new Set(["no_mapped_products", "no_costable_recipes"]);

function retryHintFor(code: string | undefined): string {
  if (code === "no_active_recipes" || code === "no_compatible_recipes") {
    return "Primero hay que añadir recetas al catálogo.";
  }
  if (code && PRICE_HINT_CODES.has(code)) {
    return "Primero hay que sincronizar precios.";
  }
  if (code && MAPPING_HINT_CODES.has(code)) {
    return "Primero hay que revisar los mapeos de productos.";
  }
  return "Primero hay que preparar los datos del planificador.";
}

export function infeasibilityView(
  diag: InfeasibilityDiagnosis | null | undefined,
): InfeasibilityView {
  const code = diag?.code;
  const canRetry = code !== undefined && RETRYABLE_CODES.has(code);

  return {
    message: infeasibilityMessage(code, diag?.reason),
    code,
    actions: (diag?.suggested_actions ?? []).map((c) => ({
      code: c,
      label: actionLabel(c),
    })),
    showBudgetAdjust: code === "genuine_budget_infeasibility",
    canRetry,
    retryHint: canRetry ? null : retryHintFor(code),
    minimumBudget: diag?.minimum_budget ?? null,
  };
}
