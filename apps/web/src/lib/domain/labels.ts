import type {
  ActionCode,
  EquipmentCode,
  MealType,
  PreflightCode,
  PriceCoverageLabel,
  ReadinessStatus,
} from "@/lib/api/types";

export const MEAL_TYPE_LABELS: Record<MealType, string> = {
  breakfast: "Desayuno",
  lunch: "Comida",
  snack: "Merienda",
  dinner: "Cena",
};

export const MEAL_TYPE_ORDER: MealType[] = ["breakfast", "lunch", "snack", "dinner"];

export const EQUIPMENT_LABELS: Record<EquipmentCode, string> = {
  oven: "Horno",
  microwave: "Microondas",
  airfryer: "Freidora de aire",
  stovetop: "Vitro / fuego",
  toaster: "Tostadora",
  pot: "Olla",
  pressure_cooker: "Olla a presión",
  blender: "Batidora",
  food_processor: "Robot de cocina",
  griddle: "Plancha",
  barbecue: "Barbacoa",
};

// The brief documents Spanish enum values (`completo`, `cobertura_parcial`…)
// but the live API observed in FASE 3.5 returns coarse English labels
// (`complete`, `partial`…) instead. Both are mapped so whichever the backend
// settles on renders a proper Spanish label instead of the raw code.
export const COVERAGE_LABELS: Record<string, string> = {
  completo: "Completo",
  complete: "Completo",
  cobertura_alta: "Cobertura alta",
  high: "Cobertura alta",
  cobertura_parcial: "Cobertura parcial",
  partial: "Cobertura parcial",
  cobertura_insuficiente: "Cobertura insuficiente",
  insufficient: "Cobertura insuficiente",
  datos_caducados: "Datos caducados",
  expired: "Datos caducados",
  sin_datos: "Sin datos",
  none: "Sin datos",
  no_data: "Sin datos",
};

export const COVERAGE_TONE: Record<string, "success" | "info" | "warning" | "error" | "neutral"> = {
  completo: "success",
  complete: "success",
  cobertura_alta: "success",
  high: "success",
  cobertura_parcial: "warning",
  partial: "warning",
  cobertura_insuficiente: "error",
  insufficient: "error",
  datos_caducados: "warning",
  expired: "warning",
  sin_datos: "neutral",
  none: "neutral",
  no_data: "neutral",
};

/** Store `price_coverage` is a decimal ratio (0–1) as a string, e.g. `"1.0000"` — format it as a percentage. */
export function formatCoveragePercent(ratio: string | null | undefined): string {
  if (ratio === null || ratio === undefined || ratio === "") return "Sin datos";
  const numeric = Number.parseFloat(ratio);
  if (Number.isNaN(numeric)) return "Sin datos";
  return `${Math.round(numeric * 100)}%`;
}

export function coverageLabel(status: PriceCoverageLabel | string | null | undefined): string {
  if (!status) return "Sin datos";
  return COVERAGE_LABELS[status] ?? status;
}

export function coverageTone(
  status: PriceCoverageLabel | string | null | undefined,
): "success" | "info" | "warning" | "error" | "neutral" {
  if (!status) return "neutral";
  return COVERAGE_TONE[status] ?? "neutral";
}

// ---------------------------------------------------------------------------
// Completed-plan price-coverage state. A plan can generate successfully yet be
// uncosted (no prices in the catalogue) or only partly costed. Rather than show
// a wall of "0,00 €" plus a raw English engine warning, we derive an honest
// state and surface a localized notice. Nothing here invents prices; it only
// describes the coverage the backend reported.
// ---------------------------------------------------------------------------

/** `none` = no prices at all (every dish uncosted); `partial` = some priced, total is indicative; `ok` = full/high coverage. */
export type PriceCoverageState = "none" | "partial" | "ok";

const COVERAGE_NONE_STATUSES = new Set(["none", "no_data", "sin_datos"]);
const COVERAGE_PARTIAL_STATUSES = new Set([
  "partial",
  "cobertura_parcial",
  "insufficient",
  "cobertura_insuficiente",
  "expired",
  "datos_caducados",
]);

export function priceCoverageState(
  coverage: { status?: string | null; price_coverage?: string | null } | null | undefined,
): PriceCoverageState {
  const status = coverage?.status ?? undefined;
  const ratio = coverage?.price_coverage;
  const numeric = ratio === null || ratio === undefined || ratio === "" ? NaN : Number.parseFloat(ratio);
  if ((status && COVERAGE_NONE_STATUSES.has(status)) || numeric === 0) return "none";
  if (
    (status && COVERAGE_PARTIAL_STATUSES.has(status)) ||
    (!Number.isNaN(numeric) && numeric > 0 && numeric < 1)
  ) {
    return "partial";
  }
  return "ok";
}

/** Honest Spanish notice shown for a completed plan whose costs are unreliable. */
export const PRICE_COVERAGE_NOTICE: Record<
  "none" | "partial",
  { tone: "info" | "warning"; title: string; body: string }
> = {
  none: {
    tone: "info",
    title: "Catálogo de precios en preparación",
    body:
      "El plan es válido, pero todavía no podemos calcular su coste: aún no hay precios " +
      "cargados para esta cadena. Cuando el catálogo tenga precios, verás aquí el coste real.",
  },
  partial: {
    tone: "warning",
    title: "Cobertura de precios parcial",
    body:
      "El coste total es orientativo: algunos platos todavía no tienen precio, así que el " +
      "coste real puede ser mayor.",
  },
};

// The pricing engine emits this warning in English; we surface our own localized
// notice instead (keyed off coverage), so the raw string is dropped to avoid a
// duplicated, untranslated message.
const BACKEND_PRICE_COVERAGE_WARNING = /price coverage is low|total cost is not reliable/i;

export function isBackendPriceCoverageWarning(warning: string): boolean {
  return BACKEND_PRICE_COVERAGE_WARNING.test(warning);
}

export const ALLERGEN_OPTIONS: { code: string; label: string }[] = [
  { code: "gluten", label: "Gluten" },
  { code: "crustaceans", label: "Crustáceos" },
  { code: "eggs", label: "Huevos" },
  { code: "fish", label: "Pescado" },
  { code: "peanuts", label: "Cacahuetes" },
  { code: "soybeans", label: "Soja" },
  { code: "milk", label: "Leche / lactosa" },
  { code: "nuts", label: "Frutos de cáscara" },
  { code: "celery", label: "Apio" },
  { code: "mustard", label: "Mostaza" },
  { code: "sesame", label: "Sésamo" },
  { code: "sulphites", label: "Sulfitos" },
  { code: "lupin", label: "Altramuces" },
  { code: "molluscs", label: "Moluscos" },
];

export const ALLERGY_SEVERITY_LABELS: Record<string, string> = {
  intolerance: "Intolerancia",
  allergy: "Alergia",
  anaphylaxis: "Anafilaxia (grave)",
};

export const PREFERENCE_TAG_OPTIONS: string[] = [
  "vegetariano",
  "vegano",
  "sin_lactosa",
  "sin_gluten",
  "picante",
  "rapido",
  "batch_cooking",
  "bajo_en_grasa",
  "alto_en_proteina",
  "mediterraneo",
  "economico",
  "sin_pescado",
];

export const PREFERENCE_TAG_LABELS: Record<string, string> = {
  vegetariano: "Vegetariano",
  vegano: "Vegano",
  sin_lactosa: "Sin lactosa",
  sin_gluten: "Sin gluten",
  picante: "Picante",
  rapido: "Rápido",
  batch_cooking: "Batch cooking",
  bajo_en_grasa: "Bajo en grasa",
  alto_en_proteina: "Alto en proteína",
  mediterraneo: "Mediterráneo",
  economico: "Económico",
  sin_pescado: "Sin pescado",
};

export const DIET_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Sin restricción particular" },
  { value: "omnivoro", label: "Omnívoro" },
  { value: "vegetariano", label: "Vegetariano" },
  { value: "vegano", label: "Vegano" },
  { value: "pescetariano", label: "Pescetariano" },
  { value: "sin_gluten", label: "Sin gluten" },
];

export const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "En cola",
  collecting_data: "Recopilando precios y catálogo",
  generating_candidates: "Generando propuestas de recetas",
  validating: "Validando alergias y restricciones",
  optimizing: "Optimizando el plan según tu presupuesto",
  completed: "Plan listo",
  failed: "No se pudo generar el plan",
  cancelled: "Generación cancelada",
};

export const RUN_STATUS_ORDER = [
  "queued",
  "collecting_data",
  "generating_candidates",
  "validating",
  "optimizing",
  "completed",
];

// ---------------------------------------------------------------------------
// Admin — data imports & catalog sources (FASE 4). Backend enum values for
// `AdminImportRecord.status` / `AdminSource.status` aren't confirmed on the
// wire (the openapi.json types them as opaque dicts), so these maps cover
// every plausible spelling and fall back to the raw code — never a blank
// label — for anything unmapped.
// ---------------------------------------------------------------------------

export const IMPORT_STATUS_LABELS: Record<string, string> = {
  pending: "Pendiente",
  previewed: "Vista previa",
  preview: "Vista previa",
  dry_run: "Vista previa",
  validated: "Validado",
  ready: "Listo para confirmar",
  committed: "Importado",
  committing: "Importando…",
  applied: "Importado",
  rolled_back: "Revertido",
  rollback: "Revertido",
  failed: "Con errores",
  error: "Con errores",
};

export const IMPORT_STATUS_TONE: Record<string, "success" | "info" | "warning" | "error" | "neutral"> = {
  pending: "neutral",
  previewed: "info",
  preview: "info",
  dry_run: "info",
  validated: "warning",
  ready: "warning",
  committed: "success",
  committing: "info",
  applied: "success",
  rolled_back: "warning",
  rollback: "warning",
  failed: "error",
  error: "error",
};

/**
 * Derives a friendly status for an import record, preferring the raw
 * `status` field when the API's wording isn't in `IMPORT_STATUS_LABELS`, but
 * always deferring to `rolled_back_at`/`committed_at` timestamps first since
 * those are unambiguous regardless of what `status` says.
 */
export function importStatusLabel(record: {
  status?: string;
  dry_run?: boolean;
  committed_at?: string | null;
  rolled_back_at?: string | null;
}): string {
  if (record.rolled_back_at) return "Revertido";
  if (record.committed_at) return "Importado";
  if (record.status) return IMPORT_STATUS_LABELS[record.status] ?? record.status;
  return record.dry_run ? "Vista previa" : "Validado";
}

export function importStatusTone(record: {
  status?: string;
  dry_run?: boolean;
  committed_at?: string | null;
  rolled_back_at?: string | null;
}): "success" | "info" | "warning" | "error" | "neutral" {
  if (record.rolled_back_at) return "warning";
  if (record.committed_at) return "success";
  const mapped = record.status ? IMPORT_STATUS_TONE[record.status] : undefined;
  if (mapped) return mapped;
  return record.dry_run ? "info" : "warning";
}

export const SOURCE_STATUS_LABELS: Record<string, string> = {
  active: "Activo",
  enabled: "Activo",
  disabled: "Desactivado",
  inactive: "Desactivado",
  experimental: "Experimental",
  community: "Comunidad",
  skeleton: "Sin implementar",
  deprecated: "Obsoleto",
};

export function sourceStatusLabel(status: string | null | undefined): string {
  if (!status) return "Desconocido";
  return SOURCE_STATUS_LABELS[status] ?? status;
}

// ---------------------------------------------------------------------------
// Planner infeasibility & readiness (FASE 9). These are compile-time
// exhaustive `Record<Enum, string>` maps so a new backend enum value fails the
// build until it has a Spanish label. The `*Label`/`*Message` helpers accept a
// raw string (whatever the wire sends) and NEVER surface a raw slug — an
// unknown code always resolves to a neutral, human-readable fallback.
// ---------------------------------------------------------------------------

export const ACTION_CODE_LABELS: Record<ActionCode, string> = {
  add_recipes: "Añadir más recetas al catálogo.",
  relax_soft_preferences: "Reducir las preferencias opcionales.",
  change_store: "Seleccionar otra cadena con catálogo disponible.",
  reduce_meals: "Reducir el número de comidas solicitadas.",
  increase_budget: "Aumentar el presupuesto.",
  configure_provider: "Configurar y sincronizar una fuente de precios.",
  review_mappings: "Revisar los productos pendientes de asociar a ingredientes.",
};

export function actionLabel(code: string): string {
  return ACTION_CODE_LABELS[code as ActionCode] ?? "Ajuste sugerido";
}

export const INFEASIBILITY_MESSAGES: Record<PreflightCode, string> = {
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

export function infeasibilityMessage(
  code: string | undefined,
  fallback?: string | null,
): string {
  if (code && code in INFEASIBILITY_MESSAGES) {
    return INFEASIBILITY_MESSAGES[code as PreflightCode];
  }
  return fallback ?? "No se pudo generar un plan viable con los datos actuales.";
}

export const READINESS_STATUS_LABELS: Record<ReadinessStatus, string> = {
  no_recipes: "Sin recetas",
  no_catalog: "Sin catálogo",
  no_prices: "Sin precios",
  pending_mappings: "Pendiente de mapeos",
  staging_only: "Solo staging",
  ready_for_review: "Preparado para revisión",
  available: "Disponible",
};

export function readinessStatusLabel(status: string): string {
  return READINESS_STATUS_LABELS[status as ReadinessStatus] ?? "Estado desconocido";
}

// ---------------------------------------------------------------------------
// Admin: provider staging → production promotion gate reasons. Some slugs
// carry a dynamic suffix (`transport_status=degraded`), so an exact match is
// tried first, then a known prefix, falling back to the raw slug — never a
// blank label.
// ---------------------------------------------------------------------------

export const PROMOTION_GATE_REASON_LABELS: Record<string, string> = {
  not_manually_approved: "No hay mapeos aprobados manualmente para este proveedor.",
  production_flags_not_set: "Los flags de producción del proveedor no están activados.",
  price_providers_disabled: "El proveedor de precios está desactivado.",
  kill_switch_on: "El interruptor de emergencia (kill switch) está activado.",
};

const PROMOTION_GATE_REASON_PREFIXES: { prefix: string; label: (value: string) => string }[] = [
  { prefix: "transport_status=", label: (v) => `Estado de transporte: ${v}.` },
  { prefix: "mapper_status=", label: (v) => `Estado del mapeador: ${v}.` },
  { prefix: "data_rights_status=", label: (v) => `Estado de derechos de datos: ${v}.` },
];

export function promotionGateReasonLabel(reason: string): string {
  const exact = PROMOTION_GATE_REASON_LABELS[reason];
  if (exact) return exact;
  for (const { prefix, label } of PROMOTION_GATE_REASON_PREFIXES) {
    if (reason.startsWith(prefix)) return label(reason.slice(prefix.length));
  }
  return reason;
}
