"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import type { z } from "zod";

import { useAuth } from "@/lib/auth/auth-context";
import { useOnboarding } from "@/lib/onboarding/onboarding-context";
import { memberFormSchema } from "@/lib/onboarding/schemas";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";

const ROLE_LABELS: Record<string, string> = {
  owner: "Propietario",
  editor: "Editor",
  viewer: "Solo lectura",
};

export default function MiembrosPage() {
  const router = useRouter();
  const { user } = useAuth();
  const { state, addMember, removeMember } = useOnboarding();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<
    z.input<typeof memberFormSchema>,
    unknown,
    z.output<typeof memberFormSchema>
  >({
    resolver: zodResolver(memberFormSchema),
    defaultValues: { display_name: "", role: "viewer", is_eater: true, relative_servings: 1 },
  });

  // Seed the wizard with the account holder as the household's first (owner) member.
  useEffect(() => {
    if (state.members.length === 0) {
      addMember({
        localId: crypto.randomUUID(),
        display_name: user?.display_name ?? "Tú",
        role: "owner",
        is_eater: true,
        relative_servings: 1,
        diet_type: null,
        allergies: [],
        intolerances: [],
        rejected_ingredients: [],
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onSubmit = handleSubmit((values) => {
    addMember({
      localId: crypto.randomUUID(),
      display_name: values.display_name,
      role: values.role,
      is_eater: values.is_eater,
      relative_servings: values.relative_servings,
      diet_type: null,
      allergies: [],
      intolerances: [],
      rejected_ingredients: [],
    });
    reset({ display_name: "", role: "viewer", is_eater: true, relative_servings: 1 });
  });

  const canContinue = state.members.length > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Miembros del hogar</CardTitle>
        <CardDescription>
          Añade a cada persona que come en el hogar. Las raciones relativas ajustan cuánto come
          cada una frente a una ración estándar (1.0).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <ul className="flex flex-col gap-2">
          {state.members.map((member) => (
            <li
              key={member.localId}
              className="flex items-center justify-between gap-3 rounded-md border border-border px-4 py-3"
            >
              <div>
                <p className="text-sm font-medium text-ink">{member.display_name}</p>
                <p className="text-xs text-ink-muted">
                  Ración relativa {member.relative_servings}× · {member.is_eater ? "Come" : "No come"}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Badge tone={member.role === "owner" ? "primary" : "neutral"}>
                  {ROLE_LABELS[member.role]}
                </Badge>
                {member.role !== "owner" ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => removeMember(member.localId)}
                    aria-label={`Quitar a ${member.display_name}`}
                  >
                    Quitar
                  </Button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>

        <form
          onSubmit={onSubmit}
          noValidate
          className="flex flex-col gap-3 rounded-md border border-dashed border-border-strong p-4"
        >
          <p className="text-sm font-semibold text-ink">Añadir miembro</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Input
              label="Nombre"
              placeholder="p. ej. Marcos"
              required
              error={errors.display_name?.message}
              {...register("display_name")}
            />
            <Select
              label="Rol"
              options={[
                { value: "editor", label: "Editor" },
                { value: "viewer", label: "Solo lectura" },
              ]}
              error={errors.role?.message}
              {...register("role")}
            />
            <Input
              label="Ración relativa"
              type="number"
              step="0.25"
              min={0.25}
              max={5}
              error={errors.relative_servings?.message}
              {...register("relative_servings")}
            />
            <label className="flex items-center gap-2 self-end pb-2.5 text-sm text-ink">
              <input type="checkbox" defaultChecked className="h-4 w-4 accent-primary" {...register("is_eater")} />
              Come en el plan
            </label>
          </div>
          <Button type="submit" variant="secondary" size="sm" className="self-start">
            Añadir a la lista
          </Button>
        </form>

        {!canContinue ? (
          <Alert tone="warning">Añade al menos un miembro para continuar.</Alert>
        ) : null}
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/onboarding/tienda")}>
          Atrás
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={!canContinue}
          onClick={() => router.push("/onboarding/alergias")}
        >
          Continuar
        </Button>
      </div>
    </Card>
  );
}
