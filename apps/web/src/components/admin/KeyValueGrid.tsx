/**
 * Renders an opaque `Record<string, unknown>` (import `counts`, `would_change`,
 * `coverage`, …) as a readable key/value grid. Several admin endpoints are
 * typed as `additionalProperties: true` on the wire — this is the fallback
 * that keeps whatever the backend actually returns visible instead of
 * silently dropped, while `humanizeKey` keeps snake_case codes legible.
 */
function humanizeKey(key: string): string {
  return key
    .replaceAll("_", " ")
    .replace(/^\w/, (char) => char.toUpperCase());
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Sí" : "No";
  if (typeof value === "number" || typeof value === "string") return String(value);
  if (Array.isArray(value)) return value.map((item) => formatValue(item)).join(", ") || "—";
  return JSON.stringify(value);
}

export interface KeyValueGridProps {
  data: Record<string, unknown> | null | undefined;
  emptyMessage?: string;
}

export function KeyValueGrid({ data, emptyMessage = "Sin datos." }: KeyValueGridProps) {
  const entries = Object.entries(data ?? {});

  if (entries.length === 0) {
    return <p className="text-sm text-ink-muted">{emptyMessage}</p>;
  }

  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
      {entries.map(([key, value]) => (
        <div key={key} className="rounded-md border border-border bg-bg-subtle px-3 py-2">
          <dt className="text-xs text-ink-muted">{humanizeKey(key)}</dt>
          <dd className="font-display text-display-sm text-ink">{formatValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
