import type { MemberCreate, PreferenceIn } from "@/lib/api/types";
import type { OnboardingMemberDraft } from "./types";

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
    allergies: member.allergies,
    intolerances: member.intolerances,
    preferences: householdPreferences,
    rejected_ingredients: member.rejected_ingredients,
  };
}
