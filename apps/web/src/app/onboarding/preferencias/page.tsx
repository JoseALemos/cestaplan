"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { PREFERENCE_TAG_LABELS, PREFERENCE_TAG_OPTIONS } from "@/lib/domain/labels";
import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { cn } from "@/lib/utils/cn";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";

const PRIORITY_WEIGHTS = [10, 7, 4];
const MAX_PRIORITY = 3;

export default function PreferenciasPage() {
  const router = useRouter();
  const { state, setPreferences } = useOnboarding();
  const [selected, setSelected] = useState<string[]>(
    state.preferences.map((preference) => preference.subject_ref),
  );

  const toggleTag = (tag: string) => {
    setSelected((prev) => {
      if (prev.includes(tag)) return prev.filter((existing) => existing !== tag);
      if (prev.length >= MAX_PRIORITY) return prev;
      return [...prev, tag];
    });
  };

  const onContinue = () => {
    setPreferences(
      selected.map((tag, index) => ({
        subject_type: "tag" as const,
        subject_ref: tag,
        sentiment: "like" as const,
        weight: PRIORITY_WEIGHTS[index] ?? 1,
      })),
    );
    router.push("/onboarding/equipamiento");
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Preferencias</CardTitle>
        <CardDescription>
          Preferencias blandas: el optimizador las prioriza, pero puede relajarlas si el
          presupuesto lo exige. Elige hasta {MAX_PRIORITY}, por orden de importancia.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          {PREFERENCE_TAG_OPTIONS.map((tag) => {
            const priorityIndex = selected.indexOf(tag);
            const isSelected = priorityIndex !== -1;
            return (
              <button
                key={tag}
                type="button"
                onClick={() => toggleTag(tag)}
                aria-pressed={isSelected}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors",
                  isSelected
                    ? "border-accent bg-accent text-accent-ink"
                    : "border-border text-ink-muted hover:bg-bg-subtle",
                )}
              >
                {isSelected ? <span aria-hidden="true">#{priorityIndex + 1}</span> : null}
                {PREFERENCE_TAG_LABELS[tag] ?? tag}
              </button>
            );
          })}
        </div>
        {selected.length >= MAX_PRIORITY ? (
          <Alert tone="info">
            Ya tienes {MAX_PRIORITY} preferencias prioritarias. Quita una para elegir otra.
          </Alert>
        ) : null}
        <p className="text-xs text-ink-faint">
          Se aplican a todo el hogar. Las alergias e ingredientes rechazados del paso anterior
          siguen siendo restricciones por persona.
        </p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/alergias")}>
          Atrás
        </Button>
        <Button type="button" size="sm" onClick={onContinue}>
          Continuar
        </Button>
      </div>
    </Card>
  );
}
