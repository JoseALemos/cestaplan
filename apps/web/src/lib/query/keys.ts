/** Central query-key factory so cache invalidation stays consistent across hooks. */
export const queryKeys = {
  me: () => ["me"] as const,
  households: () => ["households"] as const,
  household: (householdId: string) => ["households", householdId] as const,
  members: (householdId: string) => ["households", householdId, "members"] as const,
  equipment: (householdId: string) => ["households", householdId, "equipment"] as const,
  invitations: (householdId: string) =>
    ["households", householdId, "invitations"] as const,
  invitationPreview: (token: string) => ["invitations", token] as const,
  pantry: (householdId: string) => ["households", householdId, "pantry"] as const,
  ingredients: (search: string) => ["ingredients", search] as const,
  retailers: () => ["retailers"] as const,
  priceProviders: () => ["price-providers"] as const,
  stores: (retailerId: string) => ["retailers", retailerId, "stores"] as const,
  storePrices: (retailerId: string, storeId: string, page: number, search: string) =>
    ["retailers", retailerId, "stores", storeId, "prices", { page, search }] as const,
  recipe: (recipeId: string) => ["recipes", recipeId] as const,
  runStatus: (runId: string) => ["plans", "runs", runId] as const,
  plan: (mealPlanId: string) => ["plans", mealPlanId] as const,
  favorites: (householdId: string) => ["plans", "recipes", "favorites", householdId] as const,
  feedback: (householdId: string, sentiment?: string) =>
    ["plans", "recipes", "feedback", householdId, sentiment ?? "all"] as const,
  groceryList: (mealPlanId: string) => ["plans", mealPlanId, "grocery-list"] as const,
  adminSources: () => ["admin", "sources"] as const,
  adminImports: () => ["admin", "imports"] as const,
  adminImport: (importId: string) => ["admin", "imports", importId] as const,
  plannerReadiness: () => ["admin", "planner-readiness"] as const,
};
