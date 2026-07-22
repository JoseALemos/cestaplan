import { Badge } from "@/components/ui/Badge";

export interface OfflineIndicatorProps {
  isOnline: boolean;
  pendingCount: number;
}

export function OfflineIndicator({ isOnline, pendingCount }: OfflineIndicatorProps) {
  if (isOnline && pendingCount === 0) {
    return (
      <Badge tone="success" className="gap-1.5">
        <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" />
        En línea
      </Badge>
    );
  }

  if (!isOnline) {
    return (
      <Badge tone="warning" className="gap-1.5" role="status">
        <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" />
        Sin conexión — tus cambios se guardan y se sincronizan al volver
      </Badge>
    );
  }

  return (
    <Badge tone="info" className="gap-1.5" role="status">
      <span aria-hidden="true" className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      Sincronizando {pendingCount} cambio{pendingCount === 1 ? "" : "s"} pendiente
      {pendingCount === 1 ? "" : "s"}
    </Badge>
  );
}
