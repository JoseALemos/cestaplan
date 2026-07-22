import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";

export default function DespensaPage() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Despensa</CardTitle>
        <CardDescription>
          Lo que ya tienes en casa se descuenta del cálculo de compra y del coste.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Input label="Añadir producto de despensa" placeholder="p. ej. arroz, 1 kg" disabled />
        <p className="text-xs text-ink-faint">Pantalla en construcción. Este paso es opcional.</p>
      </CardContent>
      <div className="mt-2 flex items-center justify-between">
        <Link href="/onboarding/tienda">
          <Button variant="ghost" size="sm">
            Atrás
          </Button>
        </Link>
        <Link href="/onboarding/presupuesto">
          <Button size="sm">Continuar</Button>
        </Link>
      </div>
    </Card>
  );
}
