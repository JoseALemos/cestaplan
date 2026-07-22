import Link from "next/link";

import { importStatusLabel, importStatusTone } from "@/lib/domain/labels";
import { formatDateTime } from "@/lib/utils/format";
import type { AdminImportRecord } from "@/lib/api/types";

import { Badge } from "@/components/ui/Badge";

export function ImportHistoryList({ imports }: { imports: AdminImportRecord[] }) {
  if (imports.length === 0) {
    return <p className="text-sm text-ink-muted">Todavía no se ha realizado ninguna importación.</p>;
  }

  return (
    <ul className="flex flex-col gap-2">
      {imports.map((item) => (
        <li key={item.id}>
          <Link
            href={`/admin/importacion/${item.id}`}
            className="flex flex-col gap-1 rounded-md border border-border px-3.5 py-3 text-sm hover:bg-bg-subtle sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="min-w-0">
              <p className="truncate font-medium text-ink">{item.filename ?? item.id}</p>
              <p className="text-xs text-ink-faint">{formatDateTime(item.created_at)}</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {item.source_type ? <Badge tone="neutral">{item.source_type}</Badge> : null}
              <Badge tone={importStatusTone(item)}>{importStatusLabel(item)}</Badge>
            </div>
          </Link>
        </li>
      ))}
    </ul>
  );
}
