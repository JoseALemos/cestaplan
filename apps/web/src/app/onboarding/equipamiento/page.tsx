import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";

const EQUIPMENT = ["Horno", "Freidora de aire", "Microondas", "Robot de cocina", "Tupper para el trabajo"];

export default function EquipamientoPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Equipamiento de cocina</CardTitle>
        <CardDescription>
          Marca lo que tienes disponible para que las recetas propuestas sean viables.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5">
        {EQUIPMENT.map((item) => (
          <label
            key={item}
            className="flex items-center gap-3 rounded-md border border-border px-4 py-3 text-sm text-ink"
          >
            <input type="checkbox" disabled className="h-4 w-4 accent-primary" />
            {item}
          </label>
        ))}
        <p className="mt-1 text-xs text-ink-faint">Pantalla en construcción.</p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/preferencias">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/tienda">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
