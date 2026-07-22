"use client";

import { useSelectedLayoutSegment } from "next/navigation";
import type { ReactNode } from "react";

import { Stepper, type StepperStep } from "@/components/ui/Stepper";

const ONBOARDING_STEPS: StepperStep[] = [
  { id: "hogar", label: "Hogar" },
  { id: "miembros", label: "Miembros" },
  { id: "perfil-dietetico", label: "Perfil dietético" },
  { id: "alergias", label: "Alergias" },
  { id: "preferencias", label: "Preferencias" },
  { id: "equipamiento", label: "Equipamiento" },
  { id: "tienda", label: "Tienda" },
  { id: "despensa", label: "Despensa" },
  { id: "presupuesto", label: "Presupuesto" },
  { id: "comidas", label: "Comidas" },
];

export default function OnboardingLayout({ children }: { children: ReactNode }) {
  const segment = useSelectedLayoutSegment();
  const currentStepId = segment ?? ONBOARDING_STEPS[0]?.id ?? "hogar";

  return (
    <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
      <Stepper steps={ONBOARDING_STEPS} currentStepId={currentStepId} className="mb-10" />
      {children}
    </div>
  );
}
