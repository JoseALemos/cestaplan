"use client";

import { useRouter } from "next/navigation";

import { EQUIPMENT_CODES } from "@/lib/api/types";
import { EQUIPMENT_LABELS } from "@/lib/domain/labels";
import { useOnboarding } from "@/lib/onboarding/onboarding-context";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";

export default function EquipamientoPage() {
  const router = useRouter();
  const { state, setEquipment } = useOnboarding();

  const toggle = (code: (typeof EQUIPMENT_CODES)[number]) => {
    setEquipment(
      state.equipment.includes(code)
        ? state.equipment.filter((existing) => existing !== code)
        : [...state.equipment, code],
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Equipamiento de cocina</CardTitle>
        <CardDescription>
          Marca lo que tienes disponible para que las recetas propuestas sean viables.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        {EQUIPMENT_CODES.map((code) => (
          <label
            key={code}
            className="flex items-center gap-3 rounded-md border border-border px-4 py-3 text-sm text-ink"
          >
            <input
              type="checkbox"
              checked={state.equipment.includes(code)}
              onChange={() => toggle(code)}
              className="h-4 w-4 accent-primary"
            />
            {EQUIPMENT_LABELS[code]}
          </label>
        ))}
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/preferencias")}>
          Atrás
        </Button>
        <Button type="button" size="sm" onClick={() => router.push("/onboarding/presupuesto")}>
          Continuar
        </Button>
      </div>
    </Card>
  );
}
