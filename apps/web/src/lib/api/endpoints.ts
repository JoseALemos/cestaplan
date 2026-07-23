/**
 * Typed calls for the CestaPlan API surface, verified against
 * `GET /openapi.json` on the running server for every endpoint that exists
 * today. Catalog endpoints (`retailers`, `stores`, `recipes/{id}`) are coded
 * against the FASE 3.5 brief's documented shape even though they are not yet
 * in the live schema ("may be added concurrently") — call sites handle the
 * resulting 404 as a normal error state, never a crash.
 */

import { apiFetch } from "./client";
import type {
  AdminImportRecord,
  AdminSource,
  CreateAdminImportInput,
  EquipmentResponse,
  EquipmentSet,
  AcceptInvitationResponse,
  FavoriteRecipeListItem,
  FeedbackRequest,
  FeedbackSentiment,
  InvitationCreate,
  InvitationCreateResponse,
  InvitationPreviewResponse,
  InvitationResponse,
  GenerateRequest,
  GeneratePlanAccepted,
  GroceryItemIn,
  GroceryList,
  HouseholdCreate,
  HouseholdResponse,
  IngredientSuggestion,
  LoginRequest,
  LoginResponse,
  MealPlanDetail,
  MemberCreate,
  MemberResponse,
  MemberUpdate,
  OptimizationRunStatusResponse,
  PantryItemCreate,
  PantryItemResponse,
  PriceProvider,
  PantryItemUpdate,
  PasswordRecoveryRequest,
  Recipe,
  RecipeFeedbackListItem,
  RegisterRequest,
  Retailer,
  Store,
  StorePricesResponse,
  SubstituteRequest,
  UserResponse,
  Uuid,
} from "./types";

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export function registerUser(body: RegisterRequest): Promise<UserResponse> {
  return apiFetch<UserResponse>("/api/v1/auth/register", { method: "POST", body });
}

export function login(body: LoginRequest): Promise<LoginResponse> {
  return apiFetch<LoginResponse>("/api/v1/auth/login", { method: "POST", body });
}

export function logout(): Promise<void> {
  return apiFetch<void>("/api/v1/auth/logout", { method: "POST" });
}

export function getMe(): Promise<UserResponse> {
  return apiFetch<UserResponse>("/api/v1/auth/me");
}

export function requestPasswordRecovery(body: PasswordRecoveryRequest): Promise<unknown> {
  return apiFetch("/api/v1/auth/password-recovery", { method: "POST", body });
}

// ---------------------------------------------------------------------------
// Households / members / equipment
// ---------------------------------------------------------------------------

export function listHouseholds(): Promise<HouseholdResponse[]> {
  return apiFetch<HouseholdResponse[]>("/api/v1/households");
}

export function createHousehold(body: HouseholdCreate): Promise<HouseholdResponse> {
  return apiFetch<HouseholdResponse>("/api/v1/households", { method: "POST", body });
}

export function getHousehold(householdId: Uuid): Promise<HouseholdResponse> {
  return apiFetch<HouseholdResponse>(`/api/v1/households/${householdId}`);
}

export function updateHousehold(
  householdId: Uuid,
  body: Partial<HouseholdCreate>,
): Promise<HouseholdResponse> {
  return apiFetch<HouseholdResponse>(`/api/v1/households/${householdId}`, {
    method: "PATCH",
    body,
  });
}

export function addMember(householdId: Uuid, body: MemberCreate): Promise<MemberResponse> {
  return apiFetch<MemberResponse>(`/api/v1/households/${householdId}/members`, {
    method: "POST",
    body,
  });
}

export function listMembers(householdId: Uuid): Promise<MemberResponse[]> {
  return apiFetch<MemberResponse[]>(`/api/v1/households/${householdId}/members`);
}

export function updateMember(
  householdId: Uuid,
  memberId: Uuid,
  body: MemberUpdate,
): Promise<MemberResponse> {
  return apiFetch<MemberResponse>(
    `/api/v1/households/${householdId}/members/${memberId}`,
    { method: "PATCH", body },
  );
}

export function getEquipment(householdId: Uuid): Promise<EquipmentResponse[]> {
  return apiFetch<EquipmentResponse[]>(`/api/v1/households/${householdId}/equipment`);
}

export function putEquipment(
  householdId: Uuid,
  body: EquipmentSet,
): Promise<EquipmentResponse[]> {
  return apiFetch<EquipmentResponse[]>(`/api/v1/households/${householdId}/equipment`, {
    method: "PUT",
    body,
  });
}

// ---------------------------------------------------------------------------
// Invitations — invite a real user into a household with a role (link-based).
// ---------------------------------------------------------------------------

export function listInvitations(householdId: Uuid): Promise<InvitationResponse[]> {
  return apiFetch<InvitationResponse[]>(`/api/v1/households/${householdId}/invitations`);
}

export function createInvitation(
  householdId: Uuid,
  body: InvitationCreate,
): Promise<InvitationCreateResponse> {
  return apiFetch<InvitationCreateResponse>(
    `/api/v1/households/${householdId}/invitations`,
    { method: "POST", body },
  );
}

export function revokeInvitation(householdId: Uuid, invitationId: Uuid): Promise<void> {
  return apiFetch<void>(
    `/api/v1/households/${householdId}/invitations/${invitationId}`,
    { method: "DELETE" },
  );
}

export function getInvitationPreview(token: string): Promise<InvitationPreviewResponse> {
  return apiFetch<InvitationPreviewResponse>(
    `/api/v1/invitations/${encodeURIComponent(token)}`,
  );
}

export function acceptInvitation(token: string): Promise<AcceptInvitationResponse> {
  return apiFetch<AcceptInvitationResponse>(
    `/api/v1/invitations/${encodeURIComponent(token)}/accept`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Pantry (despensa) — household stock; reduces the next plan's shopping list.
// ---------------------------------------------------------------------------

export function listPantry(householdId: Uuid): Promise<PantryItemResponse[]> {
  return apiFetch<PantryItemResponse[]>(`/api/v1/households/${householdId}/pantry`);
}

export function addPantryItem(
  householdId: Uuid,
  body: PantryItemCreate,
): Promise<PantryItemResponse> {
  return apiFetch<PantryItemResponse>(`/api/v1/households/${householdId}/pantry`, {
    method: "POST",
    body,
  });
}

export function updatePantryItem(
  householdId: Uuid,
  itemId: Uuid,
  body: PantryItemUpdate,
): Promise<PantryItemResponse> {
  return apiFetch<PantryItemResponse>(
    `/api/v1/households/${householdId}/pantry/${itemId}`,
    { method: "PATCH", body },
  );
}

export function deletePantryItem(householdId: Uuid, itemId: Uuid): Promise<void> {
  return apiFetch<void>(`/api/v1/households/${householdId}/pantry/${itemId}`, {
    method: "DELETE",
  });
}

/** Canonical ingredients for the pantry autocomplete. Optional case-insensitive search. */
export function listIngredients(
  search?: string,
  limit = 20,
): Promise<IngredientSuggestion[]> {
  const query = new URLSearchParams();
  if (search) query.set("search", search);
  query.set("limit", String(limit));
  return apiFetch<IngredientSuggestion[]>(`/api/v1/ingredients?${query.toString()}`);
}

// ---------------------------------------------------------------------------
// Catalog (retailers / stores / recipe detail) — see module docblock.
// ---------------------------------------------------------------------------

export function listRetailers(): Promise<Retailer[]> {
  return apiFetch<Retailer[]>("/api/v1/retailers");
}

export function listPriceProviders(): Promise<PriceProvider[]> {
  return apiFetch<PriceProvider[]>("/api/v1/price-providers");
}

export function listStores(retailerId: Uuid): Promise<Store[]> {
  return apiFetch<Store[]>(`/api/v1/retailers/${retailerId}/stores`);
}

export function getRecipe(recipeId: Uuid): Promise<Recipe> {
  return apiFetch<Recipe>(`/api/v1/recipes/${recipeId}`);
}

export interface ListStorePricesParams {
  search?: string;
  page?: number;
  size?: number;
}

/** Real (ODbL, Open Prices) price observations for one store — the "Precios reales" viewer. */
export function listStorePrices(
  retailerId: Uuid,
  storeId: Uuid,
  params: ListStorePricesParams = {},
): Promise<StorePricesResponse> {
  const query = new URLSearchParams();
  if (params.search) query.set("search", params.search);
  if (params.page) query.set("page", String(params.page));
  if (params.size) query.set("size", String(params.size));
  const qs = query.toString();
  return apiFetch<StorePricesResponse>(
    `/api/v1/retailers/${retailerId}/stores/${storeId}/prices${qs ? `?${qs}` : ""}`,
  );
}

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

export function generatePlan(body: GenerateRequest): Promise<GeneratePlanAccepted> {
  return apiFetch<GeneratePlanAccepted>("/api/v1/plans/generate", { method: "POST", body });
}

export function getRunStatus(
  optimizationRunId: Uuid,
): Promise<OptimizationRunStatusResponse> {
  return apiFetch<OptimizationRunStatusResponse>(
    `/api/v1/plans/runs/${optimizationRunId}`,
  );
}

export function getPlan(mealPlanId: Uuid): Promise<MealPlanDetail> {
  return apiFetch<MealPlanDetail>(`/api/v1/plans/${mealPlanId}`);
}

export function regeneratePlan(mealPlanId: Uuid): Promise<GeneratePlanAccepted> {
  return apiFetch<GeneratePlanAccepted>(`/api/v1/plans/${mealPlanId}/regenerate`, {
    method: "POST",
  });
}

export function regenerateMeal(
  mealPlanId: Uuid,
  plannedMealId: Uuid,
): Promise<GeneratePlanAccepted> {
  return apiFetch<GeneratePlanAccepted>(
    `/api/v1/plans/${mealPlanId}/meals/${plannedMealId}/regenerate`,
    { method: "POST" },
  );
}

export function favoriteRecipe(recipeId: Uuid, householdId: Uuid): Promise<unknown> {
  return apiFetch(
    `/api/v1/plans/recipes/${recipeId}/favorite?household_id=${householdId}`,
    { method: "POST" },
  );
}

export function unfavoriteRecipe(recipeId: Uuid, householdId: Uuid): Promise<void> {
  return apiFetch<void>(
    `/api/v1/plans/recipes/${recipeId}/favorite?household_id=${householdId}`,
    { method: "DELETE" },
  );
}

export function submitFeedback(
  recipeId: Uuid,
  householdId: Uuid,
  body: FeedbackRequest,
): Promise<unknown> {
  return apiFetch(
    `/api/v1/plans/recipes/${recipeId}/feedback?household_id=${householdId}`,
    { method: "POST", body },
  );
}

export function clearFeedback(recipeId: Uuid, householdId: Uuid): Promise<void> {
  return apiFetch<void>(
    `/api/v1/plans/recipes/${recipeId}/feedback?household_id=${householdId}`,
    { method: "DELETE" },
  );
}

export function listFavorites(householdId: Uuid): Promise<FavoriteRecipeListItem[]> {
  return apiFetch<FavoriteRecipeListItem[]>(
    `/api/v1/plans/recipes/favorites?household_id=${householdId}`,
  );
}

export function listRecipeFeedback(
  householdId: Uuid,
  sentiment?: FeedbackSentiment,
): Promise<RecipeFeedbackListItem[]> {
  const query = new URLSearchParams({ household_id: householdId });
  if (sentiment) query.set("sentiment", sentiment);
  return apiFetch<RecipeFeedbackListItem[]>(
    `/api/v1/plans/recipes/feedback?${query.toString()}`,
  );
}

// ---------------------------------------------------------------------------
// Grocery list
// ---------------------------------------------------------------------------

export function getGroceryList(mealPlanId: Uuid): Promise<GroceryList> {
  return apiFetch<GroceryList>(`/api/v1/plans/${mealPlanId}/grocery-list`);
}

export function toggleGroceryItem(mealPlanId: Uuid, itemId: Uuid): Promise<unknown> {
  return apiFetch(
    `/api/v1/plans/${mealPlanId}/grocery-list/items/${itemId}/toggle`,
    { method: "POST" },
  );
}

export function addGroceryItem(mealPlanId: Uuid, body: GroceryItemIn): Promise<unknown> {
  return apiFetch(`/api/v1/plans/${mealPlanId}/grocery-list/items`, {
    method: "POST",
    body,
  });
}

export function substituteGroceryItem(
  mealPlanId: Uuid,
  itemId: Uuid,
  body: SubstituteRequest,
): Promise<unknown> {
  return apiFetch(
    `/api/v1/plans/${mealPlanId}/grocery-list/items/${itemId}/substitute`,
    { method: "POST", body },
  );
}

// ---------------------------------------------------------------------------
// Admin — catalog sources & data imports (admin-gated; a 403 means the
// caller isn't an admin, see `useIsAdminQuery`).
// ---------------------------------------------------------------------------

export function listAdminSources(): Promise<AdminSource[]> {
  return apiFetch<AdminSource[]>("/api/v1/admin/sources");
}

export function listAdminImports(): Promise<AdminImportRecord[]> {
  return apiFetch<AdminImportRecord[]>("/api/v1/admin/imports");
}

export function getAdminImport(importId: Uuid): Promise<AdminImportRecord> {
  return apiFetch<AdminImportRecord>(`/api/v1/admin/imports/${importId}`);
}

/** Multipart upload. `dry_run: true` validates only — nothing is written until `commitAdminImport`. */
export function createAdminImport(input: CreateAdminImportInput): Promise<AdminImportRecord> {
  const form = new FormData();
  form.set("file", input.file);
  form.set("dry_run", String(input.dry_run));
  if (input.column_mapping) {
    form.set("column_mapping", input.column_mapping);
  }
  return apiFetch<AdminImportRecord>("/api/v1/admin/imports", { method: "POST", body: form });
}

export function commitAdminImport(importId: Uuid): Promise<AdminImportRecord> {
  return apiFetch<AdminImportRecord>(`/api/v1/admin/imports/${importId}/commit`, {
    method: "POST",
  });
}

export function rollbackAdminImport(importId: Uuid): Promise<AdminImportRecord> {
  return apiFetch<AdminImportRecord>(`/api/v1/admin/imports/${importId}/rollback`, {
    method: "POST",
  });
}
