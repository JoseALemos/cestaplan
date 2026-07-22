"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { useIsAdminQuery } from "@/lib/query/hooks/use-admin";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";

/**
 * Gates every `/admin/*` screen. `GET /me` carries no `is_admin` flag on this
 * API, so admin access is derived from whether `GET /admin/sources` 403s
 * (see `useIsAdminQuery`). Not-logged-in visitors are redirected to
 * `/login`; logged-in non-admins get a friendly 403 screen in place, since
 * redirecting them would just bounce back once they retype the URL.
 */
export function AdminGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const access = useIsAdminQuery();

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [authLoading, isAuthenticated, router]);

  if (authLoading || !isAuthenticated) {
    return (
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (access.isLoading) {
    return (
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-10 sm:px-6">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (access.isForbidden) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16 sm:px-6">
        <Card>
          <CardHeader>
            <CardTitle>Acceso restringido</CardTitle>
            <CardDescription>
              Esta sección es solo para administradores de CestaPlan.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <Alert tone="warning">
              Tu cuenta ha iniciado sesión correctamente, pero no tiene permisos de
              administrador. Si crees que deberías tenerlos, contacta con quien gestione el
              despliegue.
            </Alert>
            <Link href="/households">
              <Button variant="outline" size="sm">
                Volver a mis hogares
              </Button>
            </Link>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (access.isError) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16 sm:px-6">
        <Alert tone="error" title="No se pudo comprobar el acceso">
          No se pudo conectar con la API para verificar permisos de administrador. Comprueba tu
          conexión e inténtalo de nuevo.
        </Alert>
      </div>
    );
  }

  return <>{children}</>;
}
