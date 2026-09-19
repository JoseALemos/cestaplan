"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { useHouseholdQuery } from "@/lib/query/hooks/use-households";

import { AddressForm } from "@/components/household/AddressForm";
import { HabitualSpendForm } from "@/components/household/HabitualSpendForm";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";

export default function HouseholdSettingsPage() {
  const params = useParams<{ householdId: string }>();
  const router = useRouter();
  const householdId = params.householdId;

  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const householdQuery = useHouseholdQuery(householdId);

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [authLoading, isAuthenticated, router]);

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

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-display-lg text-ink">
            Ajustes de {householdQuery.data.name}
          </h1>
          <p className="mt-1 text-ink-muted">Domicilio y datos básicos del hogar.</p>
        </div>
        <Link href="/households">
          <Button variant="ghost" size="sm">
            Volver a tus hogares
          </Button>
        </Link>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Domicilio</CardTitle>
          <CardDescription>
            Lo usamos para calcular el coste de desplazamiento a cada supermercado en la
            comparativa de precios de tus planes.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <AddressForm householdId={householdId} household={householdQuery.data} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Gasto habitual</CardTitle>
          <CardDescription>
            Cuánto sueles gastar a la semana en la compra. Con esto estimamos cuánto ahorras
            con cada plan frente a tu gasto de referencia. Es opcional y solo para ti.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HabitualSpendForm householdId={householdId} household={householdQuery.data} />
        </CardContent>
      </Card>
    </div>
  );
}
