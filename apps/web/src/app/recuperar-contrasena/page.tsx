"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import Link from "next/link";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { usePasswordRecoveryMutation } from "@/lib/query/hooks/use-auth-mutations";
import { type PasswordRecoveryFormValues, passwordRecoverySchema } from "@/lib/onboarding/schemas";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function RecuperarContrasenaPage() {
  const [submitted, setSubmitted] = useState(false);
  const recoveryMutation = usePasswordRecoveryMutation();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<PasswordRecoveryFormValues>({ resolver: zodResolver(passwordRecoverySchema) });

  const onSubmit = handleSubmit(async (values) => {
    try {
      await recoveryMutation.mutateAsync(values);
    } catch {
      // El mensaje se mantiene neutro aunque la petición falle: no debe revelar
      // si el email está o no registrado.
    } finally {
      setSubmitted(true);
    }
  });

  return (
    <div className="mx-auto max-w-md px-4 py-12 sm:px-6">
      <Card>
        <CardHeader>
          <CardTitle>Recupera tu contraseña</CardTitle>
          <CardDescription>
            Introduce tu email y te enviaremos instrucciones para restablecerla.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {submitted ? (
            <Alert tone="success">
              Si el correo está registrado, te hemos enviado instrucciones.
            </Alert>
          ) : (
            <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
              <Input
                label="Email"
                type="email"
                autoComplete="email"
                required
                error={errors.email?.message}
                {...register("email")}
              />
              <Button type="submit" loading={recoveryMutation.isPending} className="mt-1">
                Enviar instrucciones
              </Button>
            </form>
          )}
          <p className="mt-5 text-center text-sm text-ink-muted">
            <Link href="/login" className="font-medium text-primary hover:underline">
              Volver a iniciar sesión
            </Link>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
