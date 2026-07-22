import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function PresupuestoPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Presupuesto y personas</CardTitle>
        <CardDescription>
          El importe es una restricción real, no una estimación: el plan se ajusta a él.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Input label="Presupuesto objetivo (€)" type="text" inputMode="decimal" placeholder="80.00" disabled />
        <Input label="Número de comensales" type="number" placeholder="2" disabled />
        <p className="text-xs text-ink-faint">Pantalla en construcción.</p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/despensa">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/comidas">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
