"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/auth-context";
import { useHouseholdQuery, useMembersQuery } from "@/lib/query/hooks/use-households";
import {
  useCreateInvitationMutation,
  useInvitationsQuery,
  useRevokeInvitationMutation,
} from "@/lib/query/hooks/use-invitations";
import { formatDate } from "@/lib/utils/format";
import type { InvitationCreateResponse, InvitationRole } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";

const ROLE_LABELS: Record<string, string> = {
  owner: "Propietario",
  editor: "Editor",
  viewer: "Solo lectura",
};

function inviteErrorMessage(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 409) {
    return "Esa persona ya es miembro o ya tiene una invitación pendiente.";
  }
  if (error.status === 403) {
    return "Solo el propietario del hogar puede invitar a otras personas.";
  }
  if (error.status === 422) {
    return "Revisa el email introducido.";
  }
  return "No se pudo crear la invitación. Inténtalo de nuevo.";
}

export default function MiembrosPage() {
  const params = useParams<{ householdId: string }>();
  const router = useRouter();
  const householdId = params.householdId;

  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const householdQuery = useHouseholdQuery(householdId);
  const membersQuery = useMembersQuery(householdId);
  const invitationsQuery = useInvitationsQuery(householdId);
  const createInvitation = useCreateInvitationMutation(householdId);
  const revokeInvitation = useRevokeInvitationMutation(householdId);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InvitationRole>("viewer");
  const [created, setCreated] = useState<InvitationCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [authLoading, isAuthenticated, router]);

  const isOwner = householdQuery.data?.my_role === "owner";

  const acceptUrl = useMemo(() => {
    if (!created) return "";
    if (typeof window === "undefined") return created.accept_path;
    return `${window.location.origin}${created.accept_path}`;
  }, [created]);

  const onInvite = async (event: React.FormEvent) => {
    event.preventDefault();
    setCopied(false);
    try {
      const result = await createInvitation.mutateAsync({ email: email.trim(), role });
      setCreated(result);
      setEmail("");
    } catch {
      // surfaced via createInvitation.error
    }
  };

  const onCopy = async () => {
    if (!acceptUrl) return;
    try {
      await navigator.clipboard.writeText(acceptUrl);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  if (householdQuery.isLoading) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (householdQuery.isError || !householdQuery.data) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
        <Alert tone="error">No se pudo cargar este hogar.</Alert>
      </div>
    );
  }

  const invitations = invitationsQuery.data ?? [];

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-display-lg text-ink">
            Miembros de {householdQuery.data.name}
          </h1>
          <p className="mt-1 text-ink-muted">Gestiona quién forma parte del hogar y su rol.</p>
        </div>
        <Link href="/households">
          <Button variant="ghost" size="sm">
            Volver a tus hogares
          </Button>
        </Link>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Miembros actuales</CardTitle>
          <CardDescription>Roles: el propietario gestiona miembros e invitaciones.</CardDescription>
        </CardHeader>
        <CardContent>
          {membersQuery.isLoading ? (
            <Skeleton className="h-16 w-full" />
          ) : membersQuery.isError ? (
            <Alert tone="error">No se pudieron cargar los miembros.</Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {(membersQuery.data ?? []).map((member) => (
                <li
                  key={member.id}
                  className="flex items-center justify-between rounded-md border border-border px-4 py-3"
                >
                  <span className="text-ink">
                    {member.display_name ?? "Sin nombre"}
                    {member.is_eater ? "" : " · no come en casa"}
                  </span>
                  <Badge tone={member.role === "owner" ? "primary" : "neutral"}>
                    {ROLE_LABELS[member.role] ?? member.role}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {isOwner ? (
        <Card>
          <CardHeader>
            <CardTitle>Invitar a alguien</CardTitle>
            <CardDescription>
              No se envía ningún correo: genera un enlace y compártelo tú con la persona
              invitada. Solo podrá aceptarlo la cuenta con ese email.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <form onSubmit={onInvite} noValidate className="flex flex-col gap-4">
              {inviteErrorMessage(createInvitation.error) ? (
                <Alert tone="error">{inviteErrorMessage(createInvitation.error)}</Alert>
              ) : null}
              <Input
                label="Email de la persona invitada"
                type="email"
                autoComplete="off"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
              <Select
                label="Rol"
                options={[
                  { value: "viewer", label: "Solo lectura" },
                  { value: "editor", label: "Editor" },
                ]}
                value={role}
                onChange={(event) => setRole(event.target.value as InvitationRole)}
              />
              <Button type="submit" size="sm" loading={createInvitation.isPending}>
                Generar invitación
              </Button>
            </form>

            {created ? (
              <div className="flex flex-col gap-2 rounded-md border border-border bg-bg-subtle p-4">
                <p className="text-sm font-medium text-ink">
                  Enlace de invitación para {created.invitation.email}
                </p>
                <p className="break-all rounded bg-surface px-3 py-2 text-sm text-ink-muted">
                  {acceptUrl}
                </p>
                <div className="flex items-center gap-2">
                  <Button type="button" size="sm" variant="outline" onClick={onCopy}>
                    {copied ? "Copiado" : "Copiar enlace"}
                  </Button>
                  <span className="text-xs text-ink-faint">
                    Válido hasta {formatDate(created.invitation.expires_at)}. Solo se muestra
                    una vez.
                  </span>
                </div>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Invitaciones pendientes</CardTitle>
          <CardDescription>Invitaciones aún sin aceptar.</CardDescription>
        </CardHeader>
        <CardContent>
          {invitationsQuery.isLoading ? (
            <Skeleton className="h-16 w-full" />
          ) : invitationsQuery.isError ? (
            <Alert tone="error">No se pudieron cargar las invitaciones.</Alert>
          ) : invitations.length === 0 ? (
            <Alert tone="info">No hay invitaciones pendientes.</Alert>
          ) : (
            <ul className="flex flex-col gap-2">
              {invitations.map((invitation) => (
                <li
                  key={invitation.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-4 py-3"
                >
                  <div>
                    <p className="text-ink">{invitation.email}</p>
                    <p className="text-xs text-ink-muted">
                      {ROLE_LABELS[invitation.role] ?? invitation.role} · caduca{" "}
                      {formatDate(invitation.expires_at)}
                    </p>
                  </div>
                  {isOwner ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="danger"
                      loading={
                        revokeInvitation.isPending &&
                        revokeInvitation.variables === invitation.id
                      }
                      onClick={() => revokeInvitation.mutate(invitation.id)}
                    >
                      Revocar
                    </Button>
                  ) : (
                    <Badge tone="neutral">Pendiente</Badge>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
