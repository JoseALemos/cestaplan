import type { SelectOption } from "@/components/ui/Select";

// ISO 4217 — monedas soportadas en los formularios de CestaPlan. EUR es la principal (los precios
// reales provienen de retailers en España); el resto se ofrecen para hogares que presupuestan en
// otra divisa. Restringir la entrada a esta lista evita códigos inválidos (p. ej. "ABC") que antes
// solo se detectaban en el backend con un mensaje peor (F5).
export const CURRENCY_OPTIONS: SelectOption[] = [
  { value: "EUR", label: "Euro (EUR)" },
  { value: "USD", label: "Dólar estadounidense (USD)" },
  { value: "GBP", label: "Libra esterlina (GBP)" },
  { value: "CHF", label: "Franco suizo (CHF)" },
];

export const SUPPORTED_CURRENCY_CODES: string[] = CURRENCY_OPTIONS.map((c) => c.value);
