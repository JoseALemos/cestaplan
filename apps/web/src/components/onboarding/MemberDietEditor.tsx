"use client";

import { useState } from "react";

import { ALLERGEN_OPTIONS, ALLERGY_SEVERITY_LABELS, DIET_TYPE_OPTIONS } from "@/lib/domain/labels";
import type { AllergySeverity } from "@/lib/api/types";
import type { OnboardingMemberDraft } from "@/lib/onboarding/types";

import { TagListInput } from "./TagListInput";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";

export interface MemberDietEditorProps {
  member: OnboardingMemberDraft;
  onChange: (patch: Partial<OnboardingMemberDraft>) => void;
}

/** Per-member dietary profile: diet type, hard allergies (with severity), intolerances, rejected ingredients. */
export function MemberDietEditor({ member, onChange }: MemberDietEditorProps) {
  const [allergenCode, setAllergenCode] = useState("");
  const [severity, setSeverity] = useState<AllergySeverity>("allergy");

  const availableAllergens = ALLERGEN_OPTIONS.filter(
    (option) => !member.allergies.some((allergy) => allergy.allergen_code === option.code),
  );

  const addAllergy = () => {
    if (!allergenCode) return;
    onChange({
      allergies: [
        ...member.allergies,
        { allergen_code: allergenCode, severity, avoid_traces: true },
      ],
    });
    setAllergenCode("");
  };

  const removeAllergy = (code: string) => {
    onChange({ allergies: member.allergies.filter((allergy) => allergy.allergen_code !== code) });
  };

  return (
    <div className="flex flex-col gap-4">
      <Select
        label="Tipo de dieta"
        options={DIET_TYPE_OPTIONS.filter((option) => option.value !== "").map((option) => option)}
        placeholder="Sin restricción particular"
        value={member.diet_type ?? ""}
        onChange={(event) => onChange({ diet_type: event.target.value || null })}
      />

      <div className="flex flex-col gap-2">
        <p className="text-sm font-medium text-ink">Alergias (restricción dura)</p>
        <div className="flex items-end gap-2">
          <Select
            label="Alérgeno"
            placeholder="Selecciona un alérgeno"
            options={availableAllergens.map((option) => ({ value: option.code, label: option.label }))}
            value={allergenCode}
            onChange={(event) => setAllergenCode(event.target.value)}
            className="flex-1"
          />
          <Select
            label="Gravedad"
            options={(Object.entries(ALLERGY_SEVERITY_LABELS) as [AllergySeverity, string][]).map(
              ([value, label]) => ({ value, label }),
            )}
            value={severity}
            onChange={(event) => setSeverity(event.target.value as AllergySeverity)}
          />
          <Button type="button" variant="secondary" onClick={addAllergy} disabled={!allergenCode}>
            Añadir
          </Button>
        </div>
        {member.allergies.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {member.allergies.map((allergy) => {
              const label =
                ALLERGEN_OPTIONS.find((option) => option.code === allergy.allergen_code)?.label ??
                allergy.allergen_code;
              return (
                <Badge key={allergy.allergen_code} tone="error" className="gap-1.5">
                  {label} · {ALLERGY_SEVERITY_LABELS[allergy.severity ?? "allergy"]}
                  <button
                    type="button"
                    onClick={() => removeAllergy(allergy.allergen_code)}
                    aria-label={`Quitar alergia a ${label}`}
                    className="ml-0.5 text-error/70 hover:text-error"
                  >
                    ×
                  </button>
                </Badge>
              );
            })}
          </div>
        ) : null}
      </div>

      <TagListInput
        label="Intolerancias (texto libre)"
        hint="Cada una se guarda como restricción con gravedad 'intolerancia'."
        placeholder="p. ej. histamina"
        values={member.intolerances}
        onChange={(intolerances) => onChange({ intolerances })}
      />

      <TagListInput
        label="Ingredientes rechazados"
        hint="Preferencia dura de 'no me lo pongas', no es alergia."
        placeholder="p. ej. cilantro"
        values={member.rejected_ingredients}
        onChange={(rejected_ingredients) => onChange({ rejected_ingredients })}
      />
    </div>
  );
}
