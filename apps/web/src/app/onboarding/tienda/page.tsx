import Link from "next/link";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";

export default function TiendaPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Selección de tienda</CardTitle>
        <CardDescription>
          Cadena, localidad, código postal y tienda concreta. Verás la cobertura de precios antes
          de continuar.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Select
          label="Cadena"
          placeholder="Selecciona una cadena"
          options={[{ value: "demo", label: "Supermercado demo (datos sintéticos)" }]}
          disabled
        />
        <Input label="Código postal" placeholder="28001" disabled />
        <div className="flex items-center justify-between rounded-md border border-border px-4 py-3 text-sm">
          <span className="text-ink-muted">Cobertura de precios de esta tienda</span>
          <Badge tone="success">Completo (demo)</Badge>
        </div>
        <p className="text-xs text-ink-faint">Pantalla en construcción.</p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/equipamiento">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/despensa">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
