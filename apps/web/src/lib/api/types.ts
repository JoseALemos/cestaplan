/**
 * Wire types for the CestaPlan API (`apps/api`), fetched from
 * `GET /openapi.json` on the running server (127.0.0.1:8000) and hand-typed
 * here because the API returns several endpoints as `additionalProperties:
 * true` (untyped dict) — the shapes below follow the FASE 3.5 brief.
 *
 * Kept intentionally close to the wire (snake_case, money/quantities as
 * `string`) rather than remapped to camelCase: the API is Python/Pydantic,
 * every field name below is the literal JSON key, and remapping would be an
 * extra hand-maintained translation layer with no behavioural upside for an
 * internal app. Money/quantity fields are `string` on purpose — never parse
 * them into `number` for storage, only for display (see `lib/utils/format.ts`).
 */

export type Uuid = string;
export type IsoDate = string; // YYYY-MM-DD
export type IsoDateTime = string;
export type MoneyString = string;

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface RegisterRequest {
  email: string;
  password: string;
  display_name?: string | null;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface UserResponse {
  id: Uuid;
  email: string;
  display_name: string | null;
  locale: string;
  status: string;
  created_at: IsoDateTime;
}

export interface LoginResponse {
  user: UserResponse;
  csrf_token: string;
}

export interface PasswordRecoveryRequest {
  email: string;
}

// ---------------------------------------------------------------------------
// Households / members / equipment
// ---------------------------------------------------------------------------

export type HouseholdRole = "owner" | "editor" | "viewer";

export interface HouseholdCreate {
  name: string;
  currency?: string;
}

export interface HouseholdResponse {
  id: Uuid;
  name: string;
  currency: string;
  my_role: HouseholdRole;
  member_count: number;
  created_at: IsoDateTime;
}

export type AllergySeverity = "intolerance" | "allergy" | "anaphylaxis";

export interface AllergyIn {
  allergen_code: string;
  severity?: AllergySeverity;
  avoid_traces?: boolean;
  notes?: string | null;
}

export interface AllergyResponse {
  allergen_code: string;
  severity: string;
  avoid_traces: boolean;
  notes: string | null;
}

export type PreferenceSubjectType = "ingredient" | "cuisine" | "tag";
export type PreferenceSentiment = "like" | "dislike" | "avoid";

export interface PreferenceIn {
  subject_type?: PreferenceSubjectType;
  subject_ref: string;
  sentiment?: PreferenceSentiment;
  weight?: number | string | null;
}

export interface PreferenceResponse {
  subject_type: string;
  subject_ref: string;
  sentiment: string;
  weight: string | null;
}

export interface NutritionGoalIn {
  energy_target_kcal?: number | string | null;
  protein_target_g?: number | string | null;
  carb_target_g?: number | string | null;
  fat_target_g?: number | string | null;
}

export interface DietaryProfileResponse {
  diet_type: string | null;
  energy_target_kcal: string | null;
  protein_target_g: string | null;
  carb_target_g: string | null;
  fat_target_g: string | null;
  notes: string | null;
  allergies: AllergyResponse[];
  preferences: PreferenceResponse[];
}

export interface MemberCreate {
  display_name: string;
  role?: HouseholdRole;
  is_eater?: boolean;
  diet_type?: string | null;
  notes?: string | null;
  nutrition_goal?: NutritionGoalIn | null;
  allergies?: AllergyIn[];
  intolerances?: string[];
  preferences?: PreferenceIn[];
  rejected_ingredients?: string[];
}

export type MemberUpdate = Partial<MemberCreate>;

export interface MemberResponse {
  id: Uuid;
  display_name: string | null;
  role: string;
  is_eater: boolean;
  profile: DietaryProfileResponse | null;
}

export const EQUIPMENT_CODES = [
  "oven",
  "microwave",
  "airfryer",
  "stovetop",
  "toaster",
  "pot",
  "pressure_cooker",
  "blender",
  "food_processor",
  "griddle",
  "barbecue",
] as const;

export type EquipmentCode = (typeof EQUIPMENT_CODES)[number];

export interface EquipmentIn {
  equipment_code: EquipmentCode;
  available?: boolean;
}

export interface EquipmentResponse {
  equipment_code: string;
  available: boolean;
}

export interface EquipmentSet {
  equipment: EquipmentIn[];
}

// ---------------------------------------------------------------------------
// Pantry (despensa) — household stock that reduces the next plan's shopping list.
// Units are the engine's known mass/volume/count units; quantity is a string.
// ---------------------------------------------------------------------------

export const PANTRY_UNITS = ["g", "kg", "mg", "ml", "l", "cl", "unit", "ud"] as const;

export type PantryUnit = (typeof PANTRY_UNITS)[number];

export interface PantryItemResponse {
  id: Uuid;
  canonical_name: string;
  display: string;
  quantity: string;
  unit: string;
  expires_at: IsoDate | null;
}

export interface PantryItemCreate {
  name: string;
  quantity: string;
  unit: string;
  expires_at?: IsoDate | null;
}

export interface PantryItemUpdate {
  quantity?: string;
  unit?: string;
  expires_at?: IsoDate | null;
}

export interface IngredientSuggestion {
  canonical_name: string;
  display_name: string;
  default_unit: string | null;
  category_code: string | null;
}

// ---------------------------------------------------------------------------
// Catalog — retailers / stores / recipes.
// NOT present in the live openapi.json yet ("may be added concurrently" per
// the brief). Typed against the documented shape so call sites are ready;
// every screen using these degrades to an explicit error/empty state if the
// endpoint 404s today.
// ---------------------------------------------------------------------------

export interface Retailer {
  id: Uuid;
  name: string;
  is_synthetic: boolean;
}

export type PriceCoverageLabel =
  | "completo"
  | "cobertura_alta"
  | "cobertura_parcial"
  | "cobertura_insuficiente"
  | "datos_caducados"
  | "sin_datos";

export interface Store {
  id: Uuid;
  name: string;
  province: string;
  locality: string;
  postal_code: string;
  external_store_id: string;
  catalog_updated_at: IsoDateTime | null;
  /** Decimal ratio (0–1) as a string, e.g. `"1.0000"` — a coverage *ratio*, not the coarse status label used on plans/grocery lists. */
  price_coverage: string | null;
  /** Count of distinct products with at least one real price at this store. */
  priced_product_count: number;
}

// ---------------------------------------------------------------------------
// "Precios reales" viewer — real Open Prices (ODbL) observations for a store.
// Read-only; never feeds the planner (which uses the synthetic demo catalogue
// or a household's own imports). Money/quantities are `string`, as always.
// ---------------------------------------------------------------------------

export interface StorePriceItem {
  product_id: Uuid;
  product_name: string;
  brand: string | null;
  barcode: string | null;
  amount: MoneyString;
  currency: string;
  unit_price: MoneyString | null;
  package_quantity: string | null;
  package_unit: string | null;
  observed_at: IsoDate;
  source_type: string;
  source_name: string;
  source_url: string | null;
  is_synthetic: boolean;
}

export interface StorePricesStoreSummary {
  id: Uuid;
  name: string | null;
  locality: string | null;
  postal_code: string | null;
  catalog_updated_at: IsoDateTime | null;
}

export interface StorePricesResponse {
  store: StorePricesStoreSummary;
  page: number;
  size: number;
  count: number;
  items: StorePriceItem[];
  /** ODbL attribution text for the Open Prices data source — must be shown wherever these prices are displayed. */
  attribution: string | null;
  license_code: string | null;
}

export interface RecipeIngredient {
  canonical_name: string;
  display_name: string;
  quantity: string;
  unit: string;
  optional: boolean;
  substitution_group: string | null;
}

export interface RecipeStep {
  position: number;
  instruction: string;
}

export interface Recipe {
  id: Uuid;
  title: string;
  description: string | null;
  servings: number;
  meal_types: string[];
  cuisine: string | null;
  preference_tags: string[];
  preparation_minutes: number | null;
  cooking_minutes: number | null;
  required_equipment: string[];
  ingredients: RecipeIngredient[];
  steps: RecipeStep[];
  allergens: string[];
  nutrition: Record<string, string> | null;
}

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

export type MealType = "breakfast" | "lunch" | "snack" | "dinner";
export type WeekDay =
  | "monday"
  | "tuesday"
  | "wednesday"
  | "thursday"
  | "friday"
  | "saturday"
  | "sunday";

export interface MealRequirementIn {
  meal_type: MealType;
  requested_count: number;
  default_servings?: number;
  selected_dates?: IsoDate[] | null;
  auto_distribute?: boolean;
  preferred_days?: WeekDay[] | null;
  maximum_preparation_minutes?: number | null;
  requires_tupper?: boolean;
  reheating_available?: boolean;
}

/**
 * How the engine should use the budget:
 * - "waste" (default): budget is an envelope — maximize variety and low waste within it.
 * - "price": minimize cost — the cheapest plan that still meets every constraint.
 */
export type BudgetPriority = "waste" | "price";

export interface GenerateRequest {
  household_id: Uuid;
  start_date: IsoDate;
  end_date: IsoDate;
  budget_amount: MoneyString;
  currency?: string;
  /** Budget strategy. Omitted -> backend defaults to "waste" (current behavior). */
  priority?: BudgetPriority;
  /** Store to cost the plan against. Omitted -> backend uses the household's default store. */
  store_id?: Uuid | null;
  requirements: MealRequirementIn[];
}

export interface GeneratePlanAccepted {
  optimization_run_id: Uuid;
  meal_plan_id: Uuid;
  status_url?: string;
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

export interface InfeasibilityDiagnosis {
  reason?: string;
  minimum_budget?: MoneyString;
  offending_products?: { name: string; reason?: string }[];
  suggested_actions?: string[];
  [key: string]: unknown;
}

export interface OptimizationRunStatusResponse {
  status: OptimizationRunStatus;
  meal_plan_id: Uuid;
  optimization_run_id?: Uuid;
  infeasibility?: InfeasibilityDiagnosis | null;
  [key: string]: unknown;
}

/** Observed keys are English/abbreviated (`kcal`, `carbs_g`) — kept flexible for whatever the optimizer emits. */
export interface MealNutrition {
  kcal?: string;
  protein_g?: string;
  carbs_g?: string;
  fat_g?: string;
  [key: string]: string | undefined;
}

export interface PlannedMealCost {
  /** Full recipe-batch cost before splitting across servings/meals. */
  total: MoneyString;
  /** Cost newly incurred by this meal (0 if it reuses packages already bought for a sibling meal). */
  marginal: MoneyString;
  /** This meal's fair share of the total cost — the number to show as "this meal costs". */
  imputable: MoneyString;
}

export interface PlannedMeal {
  id: Uuid;
  recipe_id: Uuid;
  title: string;
  date: IsoDate;
  meal_type: MealType;
  servings: number;
  status: string;
  cost: PlannedMealCost;
  nutrition: MealNutrition | null;
  nutrition_complete: boolean;
  explanation: string | null;
}

export interface MealPlanCoverage {
  /** Coarse status label — observed values are English (`"complete"`), not the Spanish enum documented in the brief. */
  status: PriceCoverageLabel | string;
  /** Decimal ratio (0–1) as a string. */
  price_coverage: string;
  weighted_price_coverage: string;
  counts?: Record<string, number>;
}

export interface MealPlanCostTotal {
  known: MoneyString;
  total: MoneyString;
  estimated: MoneyString;
}

export interface MealPlanTotals {
  cost_total: MealPlanCostTotal;
  cost_per_day?: Record<string, MoneyString>;
  [key: string]: unknown;
}

export interface MealPlanBudget {
  amount: MoneyString;
  currency: string;
}

export interface MealPlanDetail {
  id: Uuid;
  status: string;
  start_date: IsoDate;
  end_date: IsoDate;
  budget: MealPlanBudget;
  run?: { id?: Uuid; status?: OptimizationRunStatus } | null;
  planned_meals: PlannedMeal[];
  totals: MealPlanTotals;
  budget_diff: MoneyString | number;
  coverage: MealPlanCoverage;
  warnings: string[];
  explanations?: string[];
  grocery_summary?: Record<string, unknown>;
}

export type FeedbackSentiment = "like" | "reject" | "no_show";

export interface FeedbackRequest {
  sentiment: FeedbackSentiment;
}

/** Row shape shared by the favorites/feedback list endpoints (brief recipe info). */
interface RecipeListItemBase {
  recipe_id: Uuid;
  title: string;
  meal_types: string[];
  cuisine: string | null;
  preparation_minutes: number | null;
  cooking_minutes: number | null;
  tags: string[];
}

export interface FavoriteRecipeListItem extends RecipeListItemBase {
  favorited_at: IsoDateTime;
}

export interface RecipeFeedbackListItem extends RecipeListItemBase {
  sentiment: FeedbackSentiment;
  updated_at: IsoDateTime;
}

// ---------------------------------------------------------------------------
// Grocery list
// ---------------------------------------------------------------------------

export type GroceryPriceStatus = "known" | "estimated" | "unknown" | string;

export interface GrocerySource {
  source_type: string;
  source_name: string;
  observed_at: IsoDateTime | null;
}

export interface GroceryItem {
  id: Uuid;
  generic_name: string;
  product_name: string | null;
  needed_quantity: string;
  pending_quantity: string;
  /** Observed as a plain boolean ("do we have some in the pantry?"); typed loosely in case a quantity string is ever returned instead. */
  pantry_available: boolean | string | null;
  packages_count: number | null;
  package_quantity: string | null;
  package_unit: string | null;
  unit_price: MoneyString | null;
  subtotal: MoneyString | null;
  subtotal_known: boolean;
  price_status: GroceryPriceStatus;
  availability: string | null;
  source: GrocerySource | null;
  is_checked: boolean;
}

export interface GroceryCategory {
  category: string;
  items: GroceryItem[];
}

export interface GroceryList {
  meal_plan_id: Uuid;
  currency: string;
  coverage_status: string;
  known_cost: MoneyString;
  estimated_cost: MoneyString;
  categories: GroceryCategory[];
}

export interface GroceryItemIn {
  ingredient_id?: Uuid | null;
  product_id?: Uuid | null;
  generic_name: string;
  needed_quantity: number | string;
  unit: string;
}

export interface SubstituteRequest {
  product_id: Uuid;
}

// ---------------------------------------------------------------------------
// Admin — catalog sources & data imports (FASE 4).
// The live openapi.json declares every admin response as
// `additionalProperties: true` (an untyped dict) rather than a concrete
// schema, so these shapes are hand-typed from the FASE 4 brief and kept
// deliberately loose (`Record<string, unknown>` / index signatures) where the
// exact backend fields are unconfirmed. Render code must degrade gracefully
// (optional chaining, generic key/value fallback) instead of assuming a key
// is present.
// ---------------------------------------------------------------------------

export interface AdminSourceCapabilities {
  search: boolean;
  get_product: boolean;
  get_price: boolean;
  get_availability: boolean;
  store_catalog: boolean;
  [key: string]: boolean | undefined;
}

export interface AdminSource {
  adapter_key: string;
  version: string;
  source_type: string;
  status: string;
  enabled: boolean;
  is_community: boolean;
  requires_network: boolean;
  /** Shape unconfirmed on the wire — render defensively (string, object, or list of either). */
  retailers?: unknown[];
  capabilities: AdminSourceCapabilities;
  license_code: string | null;
  attribution_text: string | null;
  data_source?: Record<string, unknown> | null;
  last_import?: Record<string, unknown> | null;
  coverage?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface AdminImportRowError {
  row: number;
  field: string | null;
  message: string;
  [key: string]: unknown;
}

/** `POST /admin/imports` multipart body. `dry_run` defaults to `true` server-side. */
export interface CreateAdminImportInput {
  file: File;
  dry_run: boolean;
  /** Raw JSON text, passed through as-is to the `column_mapping` form field. */
  column_mapping?: string | null;
}

/**
 * Shared shape for the import batch returned by create/get/commit/rollback —
 * every field beyond `id` may or may not be present depending on the import's
 * lifecycle stage (a fresh dry-run has no `committed_at`, etc.).
 */
export interface AdminImportRecord {
  id: Uuid;
  status: string;
  format?: string;
  filename?: string;
  source_type?: string;
  dry_run: boolean;
  checksum?: string;
  counts?: Record<string, number>;
  errors?: AdminImportRowError[];
  would_change?: Record<string, unknown>;
  /** Present on the detail endpoint; absent (or empty) on the list endpoint. */
  summary?: Record<string, unknown>;
  created_at: IsoDateTime;
  committed_at?: IsoDateTime | null;
  rolled_back_at?: IsoDateTime | null;
  /** Only present on the rollback response. */
  deleted_prices?: number;
  [key: string]: unknown;
}
