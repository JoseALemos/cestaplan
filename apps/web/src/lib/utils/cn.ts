type ClassValue = string | number | bigint | null | false | undefined | ClassValue[];

function flatten(input: ClassValue, out: string[]): void {
  if (!input) return;
  if (Array.isArray(input)) {
    for (const item of input) flatten(item, out);
    return;
  }
  out.push(String(input));
}

/**
 * Minimal `clsx`-style class name combinator. Kept dependency-free on
 * purpose: CestaPlan's frontend only takes on TanStack Query, React Hook
 * Form and Zod as new runtime dependencies.
 */
export function cn(...inputs: ClassValue[]): string {
  const out: string[] = [];
  for (const input of inputs) flatten(input, out);
  return out.join(" ");
}
