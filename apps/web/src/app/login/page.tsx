"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";

import { ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/auth-context";
import { useLoginMutation } from "@/lib/query/hooks/use-auth-mutations";
import { type LoginFormValues, loginSchema } from "@/lib/onboarding/schemas";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function LoginPage() {
  const router = useRouter();
  const { refetch } = useAuth();
  const loginMutation = useLoginMutation();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginFormValues>({ resolver: zodResolver(loginSchema) });

  const onSubmit = handleSubmit(async (values) => {
    try {
      await loginMutation.mutateAsync(values);
      await refetch();
      // Honour a `?next=` return path (e.g. from an invitation link). Only accept
      // same-site relative paths so it can't be abused as an open redirect.
      const next =
        typeof window !== "undefined"
          ? new URLSearchParams(window.location.search).get("next")
          : null;
      const destination = next && next.startsWith("/") && !next.startsWith("//")
        ? next
        : "/households";
      router.push(destination);
    } catch {
      // surfaced below via loginMutation.error
    }
  });

  const errorMessage =
    loginMutation.error instanceof ApiError
      ? loginMutation.error.status === 401 || loginMutation.error.status === 422
        ? "Email o contraseña incorrectos."
        : "No se pudo iniciar sesión. Inténtalo de nuevo en un momento."
      : null;

  return (
    <div className="mx-auto max-w-md px-4 py-12 sm:px-6">
      <Card>
        <CardHeader>
          <CardTitle>Inicia sesión</CardTitle>
          <CardDescription>Accede a tus hogares, planes y listas de la compra.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
            {errorMessage ? <Alert tone="error">{errorMessage}</Alert> : null}
            <Input
              label="Email"
              type="email"
              autoComplete="email"
              required
              error={errors.email?.message}
              {...register("email")}
            />
            <Input
              label="Contraseña"
              type="password"
              autoComplete="current-password"
              required
              error={errors.password?.message}
              {...register("password")}
            />
            <Button type="submit" loading={loginMutation.isPending} className="mt-1">
              Entrar
            </Button>
          </form>
          <p className="mt-5 text-center text-sm text-ink-muted">
            ¿Todavía no tienes cuenta?{" "}
            <Link href="/registro" className="font-medium text-primary hover:underline">
              Crea una gratis
            </Link>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
