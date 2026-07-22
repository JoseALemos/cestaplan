import type {
  AllergyIn,
  EquipmentCode,
  HouseholdRole,
  MealRequirementIn,
  PreferenceIn,
} from "@/lib/api/types";

/** One member row in the wizard, before it exists server-side (hence `localId`). */
export interface OnboardingMemberDraft {
  localId: string;
  display_name: string;
  role: HouseholdRole;
  is_eater: boolean;
  /**
   * Relative servings this member eats compared to a "1 ración" baseline
   * (e.g. a teenager might be 1.5). The API has no per-member serving field
   * today — `default_servings` lives at the meal-requirement level — so this
   * is kept as wizard-only bookkeeping shown in the summary, not sent to the
   * API. See `docs/PRD.md` if a future contract adds it.
   */
  relative_servings: number;
  diet_type: string | null;
  allergies: AllergyIn[];
  intolerances: string[];
  rejected_ingredients: string[];
}

export interface OnboardingHousehold {
  name: string;
  currency: string;
}

/**
 * The supermarket CHAIN (retailer) a plan is priced against. Product decision: "la tienda
 * da igual" — prices come from the whole chain, so we only capture the chain here. The
 * legacy per-store fields remain optional for backward compatibility but are unused.
 */
export interface OnboardingStore {
  retailerId: string | null;
  retailerLabel: string | null;
  storeId?: string | null;
  storeLabel?: string | null;
  province?: string | null;
  postalCode?: string | null;
}

export type BudgetMode = "strict" | "flexible";
export type BudgetPriority = "waste" | "price";

export interface OnboardingBudget {
  amount: string;
  currency: string;
  mode: BudgetMode;
  marginPercent: number;
  /** "waste" (default) maximizes variety within the budget; "price" minimizes cost. */
  priority: BudgetPriority;
}

export interface OnboardingState {
  household: OnboardingHousehold | null;
  store: OnboardingStore | null;
  members: OnboardingMemberDraft[];
  /** Up to 3 priority soft-preference tags, applied identically to every member on submit. */
  preferences: PreferenceIn[];
  equipment: EquipmentCode[];
  budget: OnboardingBudget | null;
  mealRequirements: MealRequirementIn[];
}

export const EMPTY_ONBOARDING_STATE: OnboardingState = {
  household: null,
  store: null,
  members: [],
  preferences: [],
  equipment: [],
  budget: null,
  mealRequirements: [],
};
