"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";

import { ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/auth-context";
import { useLoginMutation, useRegisterMutation } from "@/lib/query/hooks/use-auth-mutations";
import { type RegisterFormValues, registerSchema } from "@/lib/onboarding/schemas";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function RegistroPage() {
  const router = useRouter();
  const { refetch } = useAuth();
  const registerMutation = useRegisterMutation();
  const loginMutation = useLoginMutation();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<RegisterFormValues>({ resolver: zodResolver(registerSchema) });

  const onSubmit = handleSubmit(async (values) => {
    try {
      await registerMutation.mutateAsync({
        email: values.email,
        password: values.password,
        display_name: values.display_name ? values.display_name : null,
      });
      // Registration only creates the account; sign in right away so the
      // session + CSRF cookies are set before we head into onboarding.
      await loginMutation.mutateAsync({ email: values.email, password: values.password });
      await refetch();
      router.push("/onboarding/hogar");
    } catch {
      // surfaced below
    }
  });

  const pending = registerMutation.isPending || loginMutation.isPending;
  const activeError = registerMutation.error ?? loginMutation.error;
  const errorMessage =
    activeError instanceof ApiError
      ? activeError.status === 422
        ? "Revisa el email y la contraseña (mínimo 10 caracteres)."
        : activeError.status === 409 || activeError.status === 400
          ? "Ya existe una cuenta con ese email."
          : "No se pudo crear la cuenta. Inténtalo de nuevo en un momento."
      : null;

  return (
    <div className="mx-auto max-w-md px-4 py-12 sm:px-6">
      <Card>
        <CardHeader>
          <CardTitle>Crea tu cuenta</CardTitle>
          <CardDescription>
            Gratis y sin tarjeta. Después configurarás tu hogar, tu tienda y tu presupuesto.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
            {errorMessage ? <Alert tone="error">{errorMessage}</Alert> : null}
            <Input
              label="Nombre (opcional)"
              autoComplete="name"
              error={errors.display_name?.message}
              {...register("display_name")}
            />
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
              autoComplete="new-password"
              hint="Mínimo 10 caracteres."
              required
              error={errors.password?.message}
              {...register("password")}
            />
            <Input
              label="Repite la contraseña"
              type="password"
              autoComplete="new-password"
              required
              error={errors.confirmPassword?.message}
              {...register("confirmPassword")}
            />
            <Button type="submit" loading={pending} className="mt-1">
              Crear cuenta
            </Button>
          </form>
          <p className="mt-5 text-center text-sm text-ink-muted">
            ¿Ya tienes cuenta?{" "}
            <Link href="/login" className="font-medium text-primary hover:underline">
              Inicia sesión
            </Link>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
