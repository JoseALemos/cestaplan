import { z } from "zod";

import { EQUIPMENT_CODES } from "@/lib/api/types";

const decimalString = z
  .string()
  .trim()
  .min(1, "Obligatorio")
  .regex(/^\d+([.,]\d{1,2})?$/, "Usa un número, p. ej. 45.50")
  .transform((value) => value.replace(",", "."));

export const householdSchema = z.object({
  name: z.string().trim().min(1, "Dale un nombre a tu hogar").max(200),
  currency: z.string().length(3, "Código de 3 letras, p. ej. EUR"),
});
export type HouseholdFormValues = z.infer<typeof householdSchema>;

export const memberFormSchema = z.object({
  display_name: z.string().trim().min(1, "Escribe un nombre").max(200),
  role: z.enum(["owner", "editor", "viewer"]),
  is_eater: z.boolean(),
  relative_servings: z.coerce.number().min(0.25).max(5),
});
export type MemberFormValues = z.infer<typeof memberFormSchema>;

export const allergySeveritySchema = z.enum(["intolerance", "allergy", "anaphylaxis"]);

export const memberDietSchema = z.object({
  diet_type: z.string().trim().max(100).optional().or(z.literal("")),
  // Optional nutrition target (per day) — reference values the person enters themselves,
  // never a prescribed default. Limits mirror the API's NutritionGoalIn.
  energy_target_kcal: z.union([z.coerce.number().min(0).max(20000), z.literal("")]).optional(),
  protein_target_g: z.union([z.coerce.number().min(0).max(2000), z.literal("")]).optional(),
  carb_target_g: z.union([z.coerce.number().min(0).max(2000), z.literal("")]).optional(),
  fat_target_g: z.union([z.coerce.number().min(0).max(2000), z.literal("")]).optional(),
});
export type MemberDietFormValues = z.infer<typeof memberDietSchema>;

/** Nutrition-goal-only slice of `memberDietSchema`, used to edit an existing member's target
 * from household settings without touching their diet type / allergies. */
export const memberNutritionGoalSchema = memberDietSchema.pick({
  energy_target_kcal: true,
  protein_target_g: true,
  carb_target_g: true,
  fat_target_g: true,
});
export type MemberNutritionGoalFormValues = z.infer<typeof memberNutritionGoalSchema>;

export const equipmentSchema = z.object({
  equipment: z.array(z.enum(EQUIPMENT_CODES)),
});
export type EquipmentFormValues = z.infer<typeof equipmentSchema>;

export const budgetSchema = z.object({
  amount: decimalString,
  currency: z.string().length(3),
  mode: z.enum(["strict", "flexible"]),
  marginPercent: z.coerce.number().min(0).max(50),
  priority: z.enum(["waste", "price", "nutrition"]),
});
export type BudgetFormValues = z.infer<typeof budgetSchema>;

export const mealRequirementFormSchema = z.object({
  meal_type: z.enum(["breakfast", "lunch", "snack", "dinner"]),
  requested_count: z.coerce.number().int().min(0).max(100),
  default_servings: z.coerce.number().int().min(1).max(50),
  maximum_preparation_minutes: z
    .union([z.coerce.number().int().min(0).max(1440), z.literal("")])
    .optional(),
  requires_tupper: z.boolean(),
});
export type MealRequirementFormValues = z.infer<typeof mealRequirementFormSchema>;

// --- Auth ---------------------------------------------------------------

export const loginSchema = z.object({
  email: z.string().trim().min(1, "Introduce tu email").email("Email no válido"),
  password: z.string().min(1, "Introduce tu contraseña"),
});
export type LoginFormValues = z.infer<typeof loginSchema>;

export const registerSchema = z
  .object({
    email: z.string().trim().min(1, "Introduce tu email").email("Email no válido"),
    password: z
      .string()
      .min(10, "Mínimo 10 caracteres")
      .max(128, "Máximo 128 caracteres"),
    confirmPassword: z.string().min(1, "Repite la contraseña"),
    display_name: z.string().trim().max(200).optional().or(z.literal("")),
    acceptPrivacy: z.boolean(),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: "Las contraseñas no coinciden",
    path: ["confirmPassword"],
  })
  .refine((data) => data.acceptPrivacy === true, {
    message: "Debes aceptar la Política de Privacidad",
    path: ["acceptPrivacy"],
  });
export type RegisterFormValues = z.infer<typeof registerSchema>;

export const passwordRecoverySchema = z.object({
  email: z.string().trim().min(1, "Introduce tu email").email("Email no válido"),
});
export type PasswordRecoveryFormValues = z.infer<typeof passwordRecoverySchema>;

// --- Household address (FASE 2b, travel cost) ---------------------------

export const householdAddressSchema = z.object({
  address_text: z.string().trim().min(1, "Escribe el domicilio").max(300),
  postal_code: z.string().trim().max(12, "Código postal demasiado largo").optional().or(z.literal("")),
  city: z.string().trim().max(200).optional().or(z.literal("")),
});
export type HouseholdAddressFormValues = z.infer<typeof householdAddressSchema>;
