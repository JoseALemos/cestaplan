import Link from "next/link";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function MiembrosPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Miembros e invitaciones</CardTitle>
        <CardDescription>
          Invita a otras personas del hogar. Cada una tendrá un rol: propietario, editor o solo
          lectura.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex items-center justify-between rounded-md border border-border px-4 py-3">
          <span className="text-sm font-medium text-ink">Tú</span>
          <Badge tone="primary">Propietario</Badge>
        </div>
        <Input label="Invitar por email" type="email" placeholder="persona@ejemplo.com" disabled />
        <p className="text-xs text-ink-faint">
          Pantalla en construcción — el envío de invitaciones llegará con la API de hogares.
        </p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/hogar">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/perfil-dietetico">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
