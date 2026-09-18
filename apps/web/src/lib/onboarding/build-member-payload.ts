import type { MemberCreate, NutritionGoalIn, PreferenceIn } from "@/lib/api/types";
import type { OnboardingMemberDraft } from "./types";

/** Builds the `nutrition_goal` body from the draft's raw input strings, or `null` when
 * neither field was filled in (no goal). Carb/fat targets have no UI yet, so they're
 * always sent as `null` alongside whichever of kcal/protein the person entered. */
function buildNutritionGoal(member: OnboardingMemberDraft): NutritionGoalIn | null {
  const energy = member.energy_target_kcal.trim();
  const protein = member.protein_target_g.trim();
  if (!energy && !protein) return null;
  return {
    energy_target_kcal: energy || null,
    protein_target_g: protein || null,
    carb_target_g: null,
    fat_target_g: null,
  };
}

/**
 * Turns a wizard member draft into the `MemberCreate` body the API expects.
 * The household-level preference tags (screen "Preferencias") are applied
 * identically to every member — the API models preferences per member, but
 * the wizard treats them as a household-wide priority list for simplicity.
 */
export function buildMemberPayload(
  member: OnboardingMemberDraft,
  householdPreferences: PreferenceIn[],
): MemberCreate {
  return {
    display_name: member.display_name,
    role: member.role,
    is_eater: member.is_eater,
    diet_type: member.diet_type,
    nutrition_goal: buildNutritionGoal(member),
    allergies: member.allergies,
    intolerances: member.intolerances,
    preferences: householdPreferences,
    rejected_ingredients: member.rejected_ingredients,
  };
}
