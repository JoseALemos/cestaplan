import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";

export default function PerfilDieteticoPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Perfil dietético</CardTitle>
        <CardDescription>
          Cuéntanos tus objetivos nutricionales generales para orientar las recetas propuestas.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Select
          label="Objetivo principal"
          placeholder="Selecciona una opción"
          options={[
            { value: "mantenimiento", label: "Mantenimiento" },
            { value: "alta_proteina", label: "Alto en proteína" },
            { value: "bajo_calorico", label: "Bajo en calorías" },
          ]}
          disabled
        />
        <p className="text-xs text-ink-faint">
          Pantalla en construcción. Recuerda: CestaPlan ofrece información orientativa, no
          consejo médico.
        </p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/miembros">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/alergias">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
