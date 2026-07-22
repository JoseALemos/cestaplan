import Link from "next/link";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";

const PREFERENCE_TAGS = ["Vegetariano", "Sin lactosa", "Picante", "Rápido", "Batch cooking"];

export default function PreferenciasPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Restricciones y preferencias</CardTitle>
        <CardDescription>
          Preferencias blandas: el optimizador las prioriza, pero puede relajarlas si el
          presupuesto lo exige.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          {PREFERENCE_TAGS.map((tag) => (
            <Badge key={tag} tone="neutral">
              {tag}
            </Badge>
          ))}
        </div>
        <p className="text-xs text-ink-faint">Pantalla en construcción.</p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/alergias">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/equipamiento">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
