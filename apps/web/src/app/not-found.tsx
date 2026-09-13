import Link from "next/link";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";

export default function NotFound() {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-6 px-4 py-20 text-center sm:px-6">
      <div>
        <p className="font-display text-display-2xl text-primary">404</p>
        <h1 className="mt-2 font-display text-display-md text-ink">Página no encontrada</h1>
      </div>

      <Alert tone="info">
        La página que buscas no existe, se ha movido o la dirección tiene un error.
      </Alert>

      <Link href="/">
        <Button>Volver al inicio</Button>
      </Link>
    </div>
  );
}
