import type { EquipmentCode, MealType, PriceCoverageLabel } from "@/lib/api/types";

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
