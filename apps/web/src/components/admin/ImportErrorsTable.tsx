import type { AdminImportRowError } from "@/lib/api/types";

const MAX_ROWS_SHOWN = 100;

export function ImportErrorsTable({ errors }: { errors: AdminImportRowError[] }) {
  if (errors.length === 0) return null;

  const shown = errors.slice(0, MAX_ROWS_SHOWN);

  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm font-semibold text-error">
        {errors.length} error{errors.length === 1 ? "" : "es"} de validación
      </p>
      <div className="overflow-x-auto rounded-md border border-error/30">
        <table className="w-full min-w-[420px] text-left text-sm">
          <thead className="bg-error-soft text-error">
            <tr>
              <th scope="col" className="px-3 py-2 font-semibold">
                Fila
              </th>
              <th scope="col" className="px-3 py-2 font-semibold">
                Campo
              </th>
              <th scope="col" className="px-3 py-2 font-semibold">
                Mensaje
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {shown.map((error, index) => (
              <tr key={`${error.row}-${error.field}-${index}`}>
                <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-ink-muted">
                  {error.row}
                </td>
                <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-ink-muted">
                  {error.field ?? "—"}
                </td>
                <td className="px-3 py-2 text-ink">{error.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {errors.length > MAX_ROWS_SHOWN ? (
        <p className="text-xs text-ink-faint">
          Mostrando los primeros {MAX_ROWS_SHOWN} de {errors.length} errores.
        </p>
      ) : null}
    </div>
  );
}
