# @cestaplan/web

Frontend de CestaPlan: Next.js (App Router) + React + TypeScript estricto + Tailwind CSS v4,
PWA mobile-first con shell offline. Ver la promesa del producto y las 22 pantallas en
`../../docs/PRD.md` y `../../README.md`.

## Scripts

```bash
pnpm --filter @cestaplan/web dev        # servidor de desarrollo (Turbopack)
pnpm --filter @cestaplan/web build      # build de producción
pnpm --filter @cestaplan/web start      # sirve el build de producción
pnpm --filter @cestaplan/web lint       # ESLint (eslint-config-next)
pnpm --filter @cestaplan/web typecheck  # tsc --noEmit, TypeScript estricto
```

Variables de entorno relevantes (ver `.env.example` en la raíz): `NEXT_PUBLIC_API_BASE_URL`.

## Identidad visual — paleta original "Mora & Papaya"

Sección 19 del encargo pide una identidad **amigable, moderna, limpia, optimista y accesible**,
relacionada con planificación/compra/cocina, explícitamente **no clínica y no de gimnasio**, y
que no copie el verde, las formas, la tipografía ni las composiciones de ninguna app de
referencia existente.

La paleta elegida es original y se aleja tanto del verde "fitness" saturado como del habitual
naranja/ámbar editorial:

| Token | Claro | Oscuro | Uso |
|---|---|---|---|
| `--color-primary` | `#7A2E5C` (mora/mulberry) | `#F0AED3` | Marca, navegación, enlaces, icono de la app |
| `--color-accent` | `#FF6B4A` (papaya-coral) | `#FF8163` | CTAs primarios, hitos optimistas, foco |
| `--color-bg` | `#FAF9FC` (papel frío, no crema) | `#1A1520` | Fondo de página |
| `--color-surface` | `#FFFFFF` | `#251E2D` | Tarjetas, inputs |
| `--color-ink` / `--color-ink-muted` | `#211A29` / `#5B5468` | `#F5F1F9` / `#C1B8CF` | Texto principal / secundario |

**Por qué es original**: se evita deliberadamente (a) el verde saturado típico de apps de
fitness/nutrición, (b) el azul clínico de apps de salud, y (c) la estética editorial de fondo
crema + serif + acentos ámbar/terracota (un patrón muy repetido en generación de UI con IA). En
su lugar: un morado-mora cálido (evoca berenjena/frutos del bosque, ingredientes, no tecnología)
combinado con un coral-papaya vívido (apetito, optimismo, "vamos a comprar") sobre un neutro frío
tipo papel. Los degradados y el "purple-on-white gradient" genérico de IA se evitan: los colores
se usan siempre en superficies planas.

### Estados semánticos

| Estado | Color | Nota |
|---|---|---|
| `success` | `#3F7A52` (salvia/albahaca) | Verde deliberadamente apagado, no el verde neón de gimnasio |
| `warning` | `#B8792A` (ocre) | |
| `error` | `#C23B3B` | |
| `info` | `#2E6B8A` (petróleo) | |

Todos los tokens de color viven como variables CSS en `src/app/globals.css` dentro de un bloque
`@theme` (Tailwind v4), lo que genera automáticamente utilidades (`bg-primary`, `text-ink-muted`,
`border-error`, etc.). El modo oscuro redefine las mismas variables vía
`@media (prefers-color-scheme: dark)` y un `data-theme` opcional para un futuro selector manual.

### Tipografía

- **Display / titulares**: [Bricolage Grotesque](https://fonts.google.com/specimen/Bricolage+Grotesque) —
  una grotesca contemporánea con carácter (terminales suaves, cierto punto lúdico) que evita tanto
  el serif editorial (Georgia/Fraunces/Playfair) como las sans genéricas de IA (Inter, Roboto,
  Space Grotesk, system-ui).
- **Cuerpo de texto**: [Figtree](https://fonts.google.com/specimen/Figtree) — humanista, cálida,
  muy legible en pantallas pequeñas, coherente con el tono "amigable" sin caer en lo infantil.
- Cargadas vía `next/font/google` como variables (`--font-bricolage`, `--font-figtree`) y
  mapeadas a `--font-display` / `--font-sans` en el `@theme` de Tailwind.
- Escala: `text-display-sm` (1.25rem) → `text-display-2xl` (3.25rem), con `letter-spacing`
  negativo en los tamaños grandes para un titular más compacto y seguro.

### Radios, sombras y espaciado

- **Radios**: escala "squircle" amigable, nunca esquina recta clínica:
  `--radius-xs` (6px) → `--radius-2xl` (36px). Botones y campos usan `md`/`lg`; tarjetas `xl`.
- **Sombras**: tintadas con el tono de marca (`hsl(322 45% 28% / α)` en claro,
  prácticamente negro en oscuro) en vez de negro plano, para que la profundidad se sienta
  parte de la paleta.
- **Espaciado**: escala de 4px de Tailwind más tres tokens semánticos de ritmo:
  `--spacing-card` (24px), `--spacing-section-sm` (56px), `--spacing-section` (96px).
- **Movimiento**: `--ease-plan` (`cubic-bezier(0.22,1,0.36,1)`) y duraciones `fast`/`base`/`slow`
  (120/220/420ms) para transiciones de botones, barras de progreso y aperturas de menú.

### Icono de la app

Marca original en `public/icons/icon.svg` (y variantes PNG generadas a partir del mismo SVG):
una cesta estilizada con un asa curva y una insignia circular con un check, en mora sobre un
fondo cuadrado redondeado ("squircle"). No reutiliza ningún icono de carrito/cesta de apps de
compra existentes; es una composición geométrica propia con dos formas (cesta + insignia).

## PWA

- `src/app/manifest.ts` genera `/manifest.webmanifest` (convención de Next.js App Router) con
  nombre, colores de tema y los cuatro íconos (`icon.svg`, `icon-192.png`, `icon-512.png`,
  `icon-maskable-512.png`).
- `src/app/icon.svg` y `src/app/apple-icon.png` cubren favicon y icono de iOS vía las
  convenciones de archivo de Next.
- `public/sw.js` es un service worker escrito a mano que cachea el shell estático
  (`/`, el manifest, el icono) con estrategia stale-while-revalidate y dejando pasar siempre las
  peticiones a `/api/*` a la red. Las pantallas con datos (plan, lista de la compra) añadirán su
  propia estrategia offline sobre IndexedDB más adelante; este SW solo resuelve el "app shell".
- `src/app/sw-register.tsx` registra el SW **solo en producción** (`NODE_ENV === "production"`):
  en desarrollo el SW cachearía chunks de Turbopack y produciría bugs de "build viejo" muy
  confusos, así que el registro es opt-in para build/producción y no interfiere con `next dev`.

## Componentes reutilizables (`src/components/ui`)

`Button`, `Input`, `Select`, `Card` (+ subcomponentes), `Badge`, `ProgressBar`, `Stepper`,
`Alert` (mensajes persistentes en línea) y `Toast`/`ToastProvider` (notificaciones transitorias),
`Skeleton`. Todos usan las variables de tema (nunca colores hardcodeados), tienen estados de foco
visibles (`focus-visible:ring`), soportan `aria-*` (labels, `aria-invalid`, `aria-describedby`,
`aria-live`, `role="alert"/"status"`) y aceptan `ref` como prop directa (patrón de React 19, sin
`forwardRef`).

## Estructura

```
src/app/                     # App Router: layout raíz, landing (/), manifest, icon
src/app/onboarding/          # Wizard de alta: layout con <Stepper>, 10 pantallas placeholder
src/components/ui/           # Primitivas accesibles reutilizables
src/components/layout/       # SiteHeader (nav responsive) y SiteFooter (con disclaimer)
src/lib/api/                 # apiFetch (fetch wrapper) + endpoints.ts (placeholders tipados)
src/lib/query/               # getQueryClient() para TanStack Query (App Router-safe)
src/lib/utils/cn.ts          # combinador de clases sin dependencias extra
```

## Cliente de API

`src/lib/api/client.ts` centraliza `fetch`: base URL desde `NEXT_PUBLIC_API_BASE_URL`,
`credentials: "include"` (las sesiones son cookies opacas HttpOnly, nunca JWT en
`localStorage`), un hueco para la cabecera CSRF (`CSRF_HEADER_NAME`) en mutaciones, y un
`ApiError` tipado. `src/lib/api/endpoints.ts` define tipos y funciones **placeholder** (no
llamadas todavía desde ninguna pantalla): el contrato real vendrá de `packages/contracts`
(Pydantic v2 → JSON Schema → TS + Zod) cuando se fije. El dinero viaja siempre como `string`.

## Deliberadamente fuera de esta fase

Los formularios de onboarding están deshabilitados (`disabled`) y no llaman a la API: son
placeholders de enrutado y de composición visual (con `<Stepper>`) hasta que el contrato de
`packages/contracts` esté cerrado. Las pantallas de auth, plan, lista de compra y detalle de
receta no existen aún — ver `docs/ROADMAP.md`.
