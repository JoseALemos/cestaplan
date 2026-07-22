import Link from "next/link";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";

const HOW_IT_WORKS = [
  {
    step: "1",
    title: "Dinos dónde compras",
    description:
      "Cadena, provincia o localidad, código postal y tienda concreta. Verás la fecha del catálogo y qué parte de los precios tiene cobertura real.",
  },
  {
    step: "2",
    title: "Marca tu presupuesto y tu hogar",
    description:
      "Un importe objetivo, cuántas personas coméis y qué comidas necesitáis: desayunos, comidas, meriendas, cenas. Con huecos, tuppers y repeticiones si hacen falta.",
  },
  {
    step: "3",
    title: "Genera el plan",
    description:
      "El motor determinista valida alergias y restricciones, calcula envases completos y coste con procedencia. La IA, si la activas, solo propone recetas.",
  },
  {
    step: "4",
    title: "Compra con la lista",
    description:
      "Lista agrupada por categorías, con coste conocido y estimado por separado. Funciona sin conexión dentro de la tienda.",
  },
];

const PRINCIPLES = [
  {
    tone: "primary" as const,
    title: "El presupuesto es real",
    description:
      "Cada precio lleva fuente, tienda y fecha. Si no hay dato, lo decimos: nunca inventamos un precio ni lo sustituimos por cero.",
  },
  {
    tone: "error" as const,
    title: "Las alergias son innegociables",
    description:
      "Un motor determinista —no la IA— decide qué es seguro. Ninguna receta ni producto propuesto viola una alergia declarada.",
  },
  {
    tone: "accent" as const,
    title: "Se compran envases, no gramos sueltos",
    description:
      "Si una receta pide 600 g de pollo y la bandeja es de 500 g, se compran 2 bandejas. Nunca se prorratea el precio.",
  },
  {
    tone: "success" as const,
    title: "Funciona sin conexión y sin OpenAI",
    description:
      "La lista de la compra vive en tu dispositivo (IndexedDB) para usarla en el súper. Toda función crítica opera sin IA.",
  },
];

export default function LandingPage() {
  return (
    <>
      <section className="relative overflow-hidden">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 -top-24 h-72 bg-[radial-gradient(60%_60%_at_50%_0%,var(--color-primary-soft),transparent)]"
        />
        <div className="relative mx-auto max-w-4xl px-4 pb-16 pt-16 text-center sm:px-6 sm:pt-24">
          <Badge tone="accent">Código abierto · autohospedable</Badge>
          <h1 className="mt-6 text-balance font-display text-display-lg text-ink sm:text-display-2xl">
            Del presupuesto al plato,
            <br className="hidden sm:block" /> sin sorpresas en la caja
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-balance text-lg text-ink-muted">
            &ldquo;Dime dónde compras, cuánto quieres gastar, para cuántas personas y qué
            comidas necesitas. CestaPlan genera recetas, calcula los envases necesarios y
            prepara una lista de compra adaptada a una tienda concreta.&rdquo;
          </p>
          <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <Button size="lg">Crear mi primer plan</Button>
            <Button variant="outline" size="lg">
              Ver una tienda demo
            </Button>
          </div>
          <p className="mt-5 text-sm text-ink-faint">
            Sin tarjeta. El presupuesto es una restricción real, no una estimación optimista.
          </p>
        </div>
      </section>

      <section id="como-funciona" className="border-t border-border bg-surface py-16 sm:py-24">
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          <div className="max-w-2xl">
            <p className="text-sm font-semibold uppercase tracking-wide text-accent-strong">
              Cómo funciona
            </p>
            <h2 className="mt-2 font-display text-display-md text-ink">
              Cuatro pasos, un plan reproducible
            </h2>
          </div>
          <div className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
            {HOW_IT_WORKS.map((item) => (
              <Card key={item.step} className="flex flex-col gap-3">
                <span className="font-display text-2xl text-primary">{item.step}</span>
                <h3 className="font-display text-display-sm text-ink">{item.title}</h3>
                <p className="text-sm text-ink-muted">{item.description}</p>
              </Card>
            ))}
          </div>
        </div>
      </section>

      <section className="bg-bg-subtle py-16 sm:py-24">
        <div className="mx-auto max-w-5xl px-4 sm:px-6">
          <div className="grid gap-6 md:grid-cols-2">
            <Card className="border-success/30">
              <CardHeader>
                <Badge tone="success">Qué es</Badge>
                <CardTitle className="mt-1">Un planificador con conciencia de precio</CardTitle>
                <CardDescription>Reproducible, auditable, y sin dependencia de OpenAI.</CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="flex flex-col gap-2.5 text-sm text-ink-muted">
                  <li>· Motor determinista que valida restricciones y calcula coste real.</li>
                  <li>· Cálculo por envases completos, con sobrante y coste marginal.</li>
                  <li>· Cobertura de precios explícita: conocido vs. estimado, nunca disfrazado.</li>
                  <li>· PWA mobile-first con lista de la compra offline.</li>
                </ul>
              </CardContent>
            </Card>
            <Card className="border-warning/30">
              <CardHeader>
                <Badge tone="warning">Qué NO es</Badge>
                <CardTitle className="mt-1">Un comparador de precios en vivo</CardTitle>
                <CardDescription>La calidad del coste depende de los datos que cargas.</CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="flex flex-col gap-2.5 text-sm text-ink-muted">
                  <li>· No hace scraping ni elude protecciones anti-bot.</li>
                  <li>· No trae catálogos comerciales reales precargados.</li>
                  <li>· No sustituye el consejo de un profesional sanitario.</li>
                  <li>· La IA no decide seguridad de alergias ni cálculos económicos.</li>
                </ul>
              </CardContent>
            </Card>
          </div>
        </div>
      </section>

      <section id="principios" className="border-t border-border bg-surface py-16 sm:py-24">
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          <div className="max-w-2xl">
            <p className="text-sm font-semibold uppercase tracking-wide text-accent-strong">
              Principios
            </p>
            <h2 className="mt-2 font-display text-display-md text-ink">
              Reglas que no se negocian
            </h2>
          </div>
          <div className="mt-10 grid gap-5 sm:grid-cols-2">
            {PRINCIPLES.map((principle) => (
              <Card key={principle.title} className="flex flex-col gap-2">
                <Badge tone={principle.tone} className="w-fit">
                  Principio
                </Badge>
                <h3 className="font-display text-display-sm text-ink">{principle.title}</h3>
                <p className="text-sm text-ink-muted">{principle.description}</p>
              </Card>
            ))}
          </div>
        </div>
      </section>

      <section className="bg-primary py-16 sm:py-20">
        <div className="mx-auto flex max-w-3xl flex-col items-center gap-6 px-4 text-center sm:px-6">
          <h2 className="font-display text-display-lg text-primary-ink">
            Planifica tu próxima semana con un presupuesto que sí se cumple
          </h2>
          <Link href="/onboarding/hogar">
            <Button size="lg">Empezar el alta</Button>
          </Link>
        </div>
      </section>
    </>
  );
}
