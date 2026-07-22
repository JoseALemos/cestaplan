import Link from "next/link";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function AlergiasPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Alergias e intolerancias</CardTitle>
        <CardDescription>
          Esto es una restricción dura: ninguna receta ni producto del plan final la incumplirá.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Alert tone="warning" title="Restricción de seguridad">
          El motor determinista valida las alergias, nunca la inteligencia artificial.
        </Alert>
        <Input label="Añadir alergia o intolerancia" placeholder="p. ej. frutos de cáscara" disabled />
        <p className="text-xs text-ink-faint">Pantalla en construcción.</p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/perfil-dietetico">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/preferencias">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
