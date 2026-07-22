"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { useOnboarding } from "@/lib/onboarding/onboarding-context";

import { MemberDietEditor } from "@/components/onboarding/MemberDietEditor";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { cn } from "@/lib/utils/cn";

export default function AlergiasPage() {
  const router = useRouter();
  const { state, updateMember } = useOnboarding();
  const [activeLocalId, setActiveLocalId] = useState<string | null>(
    state.members[0]?.localId ?? null,
  );

  const activeMember = state.members.find((member) => member.localId === activeLocalId) ?? state.members[0];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Dietas y alergias</CardTitle>
        <CardDescription>
          Por persona. Las alergias son una restricción de seguridad: ningún plato ni producto
          del plan final la incumplirá.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Alert tone="warning" title="Restricción de seguridad">
          El motor determinista valida las alergias, nunca la inteligencia artificial.
        </Alert>

        {state.members.length === 0 ? (
          <Alert tone="error">Vuelve al paso anterior y añade al menos un miembro.</Alert>
        ) : (
          <>
            {state.members.length > 1 ? (
              <div role="tablist" aria-label="Miembro" className="flex flex-wrap gap-2">
                {state.members.map((member) => (
                  <button
                    key={member.localId}
                    type="button"
                    role="tab"
                    aria-selected={member.localId === activeMember?.localId}
                    onClick={() => setActiveLocalId(member.localId)}
                    className={cn(
                      "rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors",
                      member.localId === activeMember?.localId
                        ? "border-primary bg-primary text-primary-ink"
                        : "border-border text-ink-muted hover:bg-bg-subtle",
                    )}
                  >
                    {member.display_name}
                  </button>
                ))}
              </div>
            ) : null}

            {activeMember ? (
              <MemberDietEditor
                key={activeMember.localId}
                member={activeMember}
                onChange={(patch) => updateMember(activeMember.localId, patch)}
              />
            ) : null}
          </>
        )}
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/miembros")}>
          Atrás
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={state.members.length === 0}
          onClick={() => router.push("/onboarding/preferencias")}
        >
          Continuar
        </Button>
      </div>
    </Card>
  );
}
