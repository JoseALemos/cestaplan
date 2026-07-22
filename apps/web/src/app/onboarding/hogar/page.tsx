import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function HogarPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Tu hogar</CardTitle>
        <CardDescription>
          Dale un nombre a tu hogar. Más adelante podrás invitar a otras personas con permisos
          de editor o solo lectura.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Input
          label="Nombre del hogar"
          placeholder="p. ej. Casa de Ana y Marcos"
          hint="Solo lo verá tu hogar. Puedes cambiarlo cuando quieras."
          disabled
        />
        <p className="text-xs text-ink-faint">
          Pantalla en construcción — se conectará a la API cuando el contrato de datos esté
          cerrado.
        </p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Button variant="ghost" size="sm" disabled>
          Atrás
        </Button>
        <Link href="/onboarding/miembros">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
