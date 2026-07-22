"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

import { ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/auth-context";
import { useCurrentHouseholdId } from "@/lib/household/current-household";
import {
  useAcceptInvitationMutation,
  useInvitationPreviewQuery,
} from "@/lib/query/hooks/use-invitations";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";

const ROLE_LABELS: Record<string, string> = {
  owner: "Propietario",
  editor: "Editor",
  viewer: "Solo lectura",
};

const STATUS_MESSAGES: Record<string, string> = {
  revoked: "Esta invitación ha sido revocada y ya no es válida.",
  accepted: "Esta invitación ya ha sido aceptada.",
  expired: "Esta invitación ha caducado.",
};

export default function AcceptInvitationPage() {
  const params = useParams<{ token: string }>();
  const router = useRouter();
  const token = params.token;

  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const preview = useInvitationPreviewQuery(token, isAuthenticated);
  const acceptMutation = useAcceptInvitationMutation();
  const [, setCurrentHouseholdId] = useCurrentHouseholdId();

  // Not logged in → send to /login, remembering where to come back to.
  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      const next = encodeURIComponent(`/invitaciones/${token}`);
      router.replace(`/login?next=${next}`);
    }
  }, [authLoading, isAuthenticated, router, token]);

  const onAccept = async () => {
    try {
      const result = await acceptMutation.mutateAsync(token);
      setCurrentHouseholdId(result.household_id);
      router.push(`/households/${result.household_id}/miembros`);
    } catch {
      // surfaced via acceptMutation.error
    }
  };

  if (authLoading || !isAuthenticated || preview.isLoading) {
    return (
      <div className="mx-auto max-w-md px-4 py-12 sm:px-6">
        <Skeleton className="h-56 w-full" />
      </div>
    );
  }

  const notFound = preview.error instanceof ApiError && preview.error.status === 404;
  if (preview.isError) {
    return (
      <div className="mx-auto max-w-md px-4 py-12 sm:px-6">
        <Alert tone="error">
          {notFound
            ? "Esta invitación no existe o el enlace es incorrecto."
            : "No se pudo cargar la invitación. Inténtalo de nuevo."}
        </Alert>
      </div>
    );
  }

  const data = preview.data;
  if (!data) return null;

  const statusMessage = STATUS_MESSAGES[data.status];
  const isPending = data.status === "pending";

  const acceptErrorMessage =
    acceptMutation.error instanceof ApiError
      ? acceptMutation.error.status === 403
        ? "Esta invitación es para otra cuenta de correo. Inicia sesión con el email invitado."
        : acceptMutation.error.status === 409
          ? "La invitación ya no está disponible o ya eres miembro de este hogar."
          : acceptMutation.error.status === 410
            ? "Esta invitación ha caducado."
            : "No se pudo aceptar la invitación. Inténtalo de nuevo."
      : null;

  return (
    <div className="mx-auto max-w-md px-4 py-12 sm:px-6">
      <Card>
        <CardHeader>
          <CardTitle>Invitación a un hogar</CardTitle>
          <CardDescription>
            Te han invitado a unirte al hogar <strong>{data.household_name}</strong>.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center justify-between rounded-md border border-border px-4 py-3">
            <span className="text-ink-muted">Rol asignado</span>
            <Badge tone="primary">{ROLE_LABELS[data.role] ?? data.role}</Badge>
          </div>

          {statusMessage ? <Alert tone="warning">{statusMessage}</Alert> : null}

          {isPending && !data.email_matches ? (
            <Alert tone="warning">
              Esta invitación es para <strong>{data.email}</strong>. Inicia sesión con esa
              cuenta para aceptarla.
            </Alert>
          ) : null}

          {acceptErrorMessage ? <Alert tone="error">{acceptErrorMessage}</Alert> : null}

          <div className="flex items-center justify-between">
            <Button variant="ghost" size="sm" onClick={() => router.push("/households")}>
              Ahora no
            </Button>
            <Button
              size="sm"
              loading={acceptMutation.isPending}
              disabled={!isPending || !data.email_matches}
              onClick={onAccept}
            >
              Aceptar invitación
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
