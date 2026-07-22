"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { ApiError } from "@/lib/api/client";
import {
  addMember,
  createHousehold,
  generatePlan,
  listMembers,
  putEquipment,
  updateMember as updateMemberApi,
} from "@/lib/api/endpoints";
import { EQUIPMENT_CODES } from "@/lib/api/types";
import { ALLERGEN_OPTIONS, DIET_TYPE_OPTIONS, EQUIPMENT_LABELS, MEAL_TYPE_LABELS, PREFERENCE_TAG_LABELS } from "@/lib/domain/labels";
import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { buildMemberPayload } from "@/lib/onboarding/build-member-payload";
import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { addDaysIso, formatMoney, todayIso } from "@/lib/utils/format";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";

function dietTypeLabel(value: string | null): string {
  return DIET_TYPE_OPTIONS.find((option) => option.value === (value ?? ""))?.label ?? value ?? "—";
}

export default function ResumenPage() {
  const router = useRouter();
  const { state, reset } = useOnboarding();
  const [, setCurrentHouseholdId] = useCurrentHouseholdId();

  const [status, setStatus] = useState<"idle" | "running" | "error">("idle");
  const [stage, setStage] = useState<string>("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const createdHouseholdIdRef = useRef<string | null>(null);
  const addedMemberIdsRef = useRef<Set<string>>(new Set());
  const equipmentDoneRef = useRef(false);

  const missing: string[] = [];
  if (!state.household) missing.push("Hogar");
  if (state.members.length === 0) missing.push("Miembros");
  if (!state.budget) missing.push("Presupuesto");
  if (state.mealRequirements.length === 0) missing.push("Comidas");

  const canGenerate = missing.length === 0;

  const handleGenerate = async () => {
    if (!state.household || !state.budget) return;
    setStatus("running");
    setErrorMessage(null);
    try {
      let householdId = createdHouseholdIdRef.current;
      if (!householdId) {
        setStage("Creando tu hogar…");
        const household = await createHousehold({
          name: state.household.name,
          currency: state.household.currency,
        });
        householdId = household.id;
        createdHouseholdIdRef.current = householdId;
        setCurrentHouseholdId(householdId);
      }

      setStage("Añadiendo miembros…");
      // Creating a household auto-adds its own "owner" member row (no
      // dietary profile yet). Reuse that row for the wizard's owner draft
      // via PATCH instead of POSTing a duplicate owner member.
      let existingOwnerMemberId: string | null = null;
      if (state.members.some((member) => member.role === "owner")) {
        const existingMembers = await listMembers(householdId);
        existingOwnerMemberId = existingMembers.find((member) => member.role === "owner")?.id ?? null;
      }
      for (const member of state.members) {
        if (addedMemberIdsRef.current.has(member.localId)) continue;
        const payload = buildMemberPayload(member, state.preferences);
        if (member.role === "owner" && existingOwnerMemberId) {
          await updateMemberApi(householdId, existingOwnerMemberId, payload);
        } else {
          await addMember(householdId, payload);
        }
        addedMemberIdsRef.current.add(member.localId);
      }

      setStage("Guardando tu equipamiento de cocina…");
      if (!equipmentDoneRef.current) {
        await putEquipment(householdId, {
          equipment: EQUIPMENT_CODES.map((code) => ({
            equipment_code: code,
            available: state.equipment.includes(code),
          })),
        });
        equipmentDoneRef.current = true;
      }

      setStage("Generando tu plan…");
      const start = todayIso();
      const accepted = await generatePlan({
        household_id: householdId,
        start_date: start,
        end_date: addDaysIso(start, 6),
        budget_amount: state.budget.amount,
        currency: state.budget.currency,
        requirements: state.mealRequirements,
      });

      reset();
      router.push(
        `/planes/estado/${accepted.optimization_run_id}?mealPlanId=${accepted.meal_plan_id}&householdId=${householdId}`,
      );
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.status === 401
            ? "Necesitas iniciar sesión para crear un hogar y generar un plan."
            : error.status === 422
              ? "Alguno de los datos no cumple lo que exige la API. Revisa los pasos anteriores."
              : `La API respondió con un error (${error.status}). Puedes reintentar: lo que ya se guardó no se repetirá.`
          : "No se pudo conectar con la API. Comprueba tu conexión y reintenta.";
      setErrorMessage(message);
      setStatus("error");
    }
  };

  const needsLogin =
    status === "error" && errorMessage?.includes("iniciar sesión");

  return (
    <Card>
      <CardHeader>
        <CardTitle>Resumen antes de generar</CardTitle>
        <CardDescription>Revisa todo antes de crear tu hogar y generar el plan.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {!canGenerate ? (
          <Alert tone="warning" title="Faltan datos">
            Completa antes: {missing.join(", ")}.
          </Alert>
        ) : null}

        <section className="flex flex-col gap-1.5 rounded-md border border-border p-4">
          <p className="text-sm font-semibold text-ink">Hogar</p>
          <p className="text-sm text-ink-muted">
            {state.household ? `${state.household.name} · ${state.household.currency}` : "—"}
          </p>
        </section>

        <section className="flex flex-col gap-1.5 rounded-md border border-border p-4">
          <p className="text-sm font-semibold text-ink">Tienda</p>
          <p className="text-sm text-ink-muted">
            {state.store?.storeLabel
              ? `${state.store.storeLabel} · ${state.store.province ?? ""} ${state.store.postalCode ?? ""}`
              : "Sin seleccionar (podrás elegirla más adelante)"}
          </p>
        </section>

        <section className="flex flex-col gap-2 rounded-md border border-border p-4">
          <p className="text-sm font-semibold text-ink">Miembros</p>
          {state.members.map((member) => (
            <div key={member.localId} className="flex flex-col gap-1 border-t border-border pt-2 first:border-t-0 first:pt-0">
              <div className="flex items-center justify-between text-sm">
                <span className="font-medium text-ink">{member.display_name}</span>
                <span className="text-ink-muted">Ración {member.relative_servings}× · {dietTypeLabel(member.diet_type)}</span>
              </div>
              {member.allergies.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {member.allergies.map((allergy) => (
                    <Badge key={allergy.allergen_code} tone="error">
                      {ALLERGEN_OPTIONS.find((option) => option.code === allergy.allergen_code)?.label ??
                        allergy.allergen_code}
                    </Badge>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </section>

        <section className="flex flex-col gap-1.5 rounded-md border border-border p-4">
          <p className="text-sm font-semibold text-ink">Preferencias prioritarias</p>
          <div className="flex flex-wrap gap-1.5">
            {state.preferences.length === 0 ? (
              <span className="text-sm text-ink-muted">Ninguna</span>
            ) : (
              state.preferences.map((preference) => (
                <Badge key={preference.subject_ref} tone="accent">
                  {PREFERENCE_TAG_LABELS[preference.subject_ref] ?? preference.subject_ref}
                </Badge>
              ))
            )}
          </div>
        </section>

        <section className="flex flex-col gap-1.5 rounded-md border border-border p-4">
          <p className="text-sm font-semibold text-ink">Equipamiento</p>
          <div className="flex flex-wrap gap-1.5">
            {state.equipment.length === 0 ? (
              <span className="text-sm text-ink-muted">Ninguno marcado</span>
            ) : (
              state.equipment.map((code) => (
                <Badge key={code} tone="neutral">
                  {EQUIPMENT_LABELS[code]}
                </Badge>
              ))
            )}
          </div>
        </section>

        <section className="flex flex-col gap-1.5 rounded-md border border-border p-4">
          <p className="text-sm font-semibold text-ink">Presupuesto</p>
          <p className="text-sm text-ink-muted">
            {state.budget
              ? `${formatMoney(state.budget.amount, state.budget.currency)} · ${
                  state.budget.mode === "strict" ? "Estricto" : `Flexible (+${state.budget.marginPercent}%)`
                }`
              : "—"}
          </p>
        </section>

        <section className="flex flex-col gap-1.5 rounded-md border border-border p-4">
          <p className="text-sm font-semibold text-ink">Comidas</p>
          {state.mealRequirements.length === 0 ? (
            <p className="text-sm text-ink-muted">Ninguna</p>
          ) : (
            <ul className="flex flex-col gap-1 text-sm text-ink-muted">
              {state.mealRequirements.map((requirement) => (
                <li key={requirement.meal_type}>
                  {MEAL_TYPE_LABELS[requirement.meal_type]}: {requirement.requested_count} ×{" "}
                  {requirement.default_servings} ración(es)
                  {requirement.requires_tupper ? " · con tupper" : ""}
                </li>
              ))}
            </ul>
          )}
        </section>

        {status === "error" && errorMessage ? (
          <Alert tone="error">
            {errorMessage}
            {needsLogin ? (
              <>
                {" "}
                <Link href="/login" className="underline">
                  Ir a iniciar sesión
                </Link>
                . Tu progreso se ha guardado en este dispositivo.
              </>
            ) : null}
          </Alert>
        ) : null}
        {status === "running" ? <Alert tone="info">{stage}</Alert> : null}
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={status === "running"}
          onClick={() => router.push("/onboarding/comidas")}
        >
          Atrás
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={!canGenerate || status === "running"}
          loading={status === "running"}
          onClick={handleGenerate}
        >
          {status === "error" ? "Reintentar" : "Generar plan"}
        </Button>
      </div>
    </Card>
  );
}
