/** Central query-key factory so cache invalidation stays consistent across hooks. */
export const queryKeys = {
  me: () => ["me"] as const,
  households: () => ["households"] as const,
  household: (householdId: string) => ["households", householdId] as const,
  members: (householdId: string) => ["households", householdId, "members"] as const,
  equipment: (householdId: string) => ["households", householdId, "equipment"] as const,
  retailers: () => ["retailers"] as const,
  stores: (retailerId: string) => ["retailers", retailerId, "stores"] as const,
  recipe: (recipeId: string) => ["recipes", recipeId] as const,
  runStatus: (runId: string) => ["plans", "runs", runId] as const,
  plan: (mealPlanId: string) => ["plans", mealPlanId] as const,
  groceryList: (mealPlanId: string) => ["plans", mealPlanId, "grocery-list"] as const,
  adminSources: () => ["admin", "sources"] as const,
  adminImports: () => ["admin", "imports"] as const,
  adminImport: (importId: string) => ["admin", "imports", importId] as const,
};
