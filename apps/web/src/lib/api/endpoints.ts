/**
 * Typed placeholders for the CestaPlan API surface.
 *
 * These types intentionally stay minimal and are NOT wired to real
 * screens yet: the API contract lives in `packages/contracts` (Pydantic v2
 * → JSON Schema → TS types + Zod schemas) and hasn't been generated for
 * this vertical slice. Once it has, replace these hand-written types with
 * the generated ones and back these functions with `apiFetch` + TanStack
 * Query hooks.
 *
 * Money fields are `string` on purpose — see docs/PRD.md, "el dinero viaja
 * como string" — never `number`/`float`.
 */

import { apiFetch } from "./client";

export type Uuid = string;

export type HouseholdRole = "owner" | "editor" | "viewer";

export interface HouseholdMemberSummary {
  id: Uuid;
  displayName: string;
  role: HouseholdRole;
}

export interface HouseholdSummary {
  id: Uuid;
  name: string;
  memberCount: number;
  members: HouseholdMemberSummary[];
}

export type OptimizationRunStatus =
  | "queued"
  | "collecting_data"
  | "generating_candidates"
  | "validating"
  | "optimizing"
  | "completed"
  | "failed"
  | "cancelled";

export interface OptimizationRunStatusResponse {
  optimizationRunId: Uuid;
  status: OptimizationRunStatus;
  /** Populated only once status is "failed" or the run has no solution. */
  error?: string;
}

export type PriceCoverageLabel =
  | "completo"
  | "cobertura_alta"
  | "cobertura_parcial"
  | "cobertura_insuficiente"
  | "datos_caducados"
  | "sin_datos";

export interface MealPlanCostSummary {
  /** String money, e.g. "42.90" — never a float. */
  knownCost: string;
  estimatedCost: string;
  currency: "EUR";
  priceCoverage: number;
  weightedPriceCoverage: number;
  coverageLabel: PriceCoverageLabel;
}

/**
 * Placeholder — NOT called from any screen yet. Present so the shape of a
 * future call site is visible and typed end to end.
 */
export async function getHousehold(householdId: Uuid): Promise<HouseholdSummary> {
  return apiFetch<HouseholdSummary>(`/api/v1/households/${householdId}`);
}

/**
 * Placeholder — NOT called from any screen yet.
 */
export async function getOptimizationRunStatus(
  optimizationRunId: Uuid,
): Promise<OptimizationRunStatusResponse> {
  return apiFetch<OptimizationRunStatusResponse>(
    `/api/v1/optimization-runs/${optimizationRunId}`,
  );
}
