"use client";

import { useEffect } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Registro solo en consola de desarrollo/servidor; nunca se muestra al usuario.
    console.error(error);
  }, [error]);

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-6 px-4 py-20 text-center sm:px-6">
      <h1 className="font-display text-display-md text-ink">Algo ha ido mal</h1>

      <Alert tone="error">
        Ha ocurrido un error inesperado. Puedes intentarlo de nuevo; si el problema
        persiste, vuelve a intentarlo más tarde.
      </Alert>

      <Button onClick={() => reset()}>Reintentar</Button>
    </div>
  );
}
