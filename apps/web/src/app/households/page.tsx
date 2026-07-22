"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { useHouseholdsQuery } from "@/lib/query/hooks/use-households";
import { formatDate } from "@/lib/utils/format";

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

export default function HouseholdsPage() {
  const router = useRouter();
  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const householdsQuery = useHouseholdsQuery();
  const [, setCurrentHouseholdId] = useCurrentHouseholdId();

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [authLoading, isAuthenticated, router]);

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-display-lg text-ink">Tus hogares</h1>
          <p className="mt-1 text-ink-muted">Elige un hogar para generar un plan nuevo.</p>
        </div>
        <Link href="/onboarding/hogar">
          <Button>Crear hogar nuevo</Button>
        </Link>
      </div>

      {householdsQuery.isLoading ? (
        <div className="flex flex-col gap-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : householdsQuery.isError ? (
        <Alert tone="error">No se pudieron cargar tus hogares. Comprueba tu conexión.</Alert>
      ) : (householdsQuery.data ?? []).length === 0 ? (
        <Alert tone="info">
          Todavía no tienes ningún hogar.{" "}
          <Link href="/onboarding/hogar" className="underline">
            Crea el primero
          </Link>
          .
        </Alert>
      ) : (
        <ul className="flex flex-col gap-3">
          {householdsQuery.data?.map((household) => (
            <li key={household.id}>
              <Card className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="font-display text-display-sm text-ink">{household.name}</p>
                  <p className="text-sm text-ink-muted">
                    {household.member_count} miembro(s) · {household.currency} · desde{" "}
                    {formatDate(household.created_at)}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge tone={household.my_role === "owner" ? "primary" : "neutral"}>
                    {ROLE_LABELS[household.my_role] ?? household.my_role}
                  </Badge>
                  <Button
                    size="sm"
                    onClick={() => {
                      setCurrentHouseholdId(household.id);
                      router.push(`/households/${household.id}/generar`);
                    }}
                  >
                    Generar plan
                  </Button>
                </div>
              </Card>
            </li>
          ))}
        </ul>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Favoritos y rechazados</CardTitle>
          <CardDescription>Recetas que has marcado desde tus planes.</CardDescription>
        </CardHeader>
        <CardContent>
          <Link href="/favoritos">
            <Button variant="outline" size="sm">
              Ver favoritos y rechazados
            </Button>
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
