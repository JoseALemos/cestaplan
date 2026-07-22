import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

const MEAL_TYPES = ["Desayunos", "Comidas", "Meriendas", "Cenas"];

export default function ComidasPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Comidas requeridas</CardTitle>
        <CardDescription>
          No hace falta llenar todos los huecos: puedes dejar días libres, pedir tuppers o
          repetir comida.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {MEAL_TYPES.map((meal) => (
          <div key={meal} className="grid grid-cols-2 gap-3">
            <Input label={meal} type="number" placeholder="0" disabled />
            <Input label="Raciones" type="number" placeholder="2" disabled />
          </div>
        ))}
        <p className="text-xs text-ink-faint">
          Pantalla en construcción — último paso del alta antes de generar el plan.
        </p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/presupuesto">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Button size="sm" disabled>
          Generar plan (próximamente)
        </Button>
      </div>
    </Card>
  );
}
