import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils/cn";

const TONE_CONFIG = {
  info: { classes: "bg-info-soft text-info border-info/20", icon: "i" },
  success: { classes: "bg-success-soft text-success border-success/20", icon: "check" },
  warning: { classes: "bg-warning-soft text-warning border-warning/20", icon: "warn" },
  error: { classes: "bg-error-soft text-error border-error/20", icon: "warn" },
} as const;

export type AlertTone = keyof typeof TONE_CONFIG;

export interface AlertProps extends HTMLAttributes<HTMLDivElement> {
  tone?: AlertTone;
  title?: string;
  children: ReactNode;
}

function AlertIcon({ tone }: { tone: AlertTone }) {
  if (tone === "success") {
    return (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <path
          d="M4 10.5l3.5 3.5L16 5"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  if (tone === "info") {
    return (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <circle cx="10" cy="10" r="7.25" stroke="currentColor" strokeWidth="1.6" />
        <path d="M10 9v5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="10" cy="6.3" r="1" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M10 2.5l8.5 15h-17L10 2.5z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M10 8v4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="10" cy="14.6" r="1" fill="currentColor" />
    </svg>
  );
}

/**
 * Inline banner for contextual, persistent messaging (form-level errors,
 * price-coverage caveats, the "no hay solución" explanation screen). For
 * transient one-off notices, see `Toast`.
 */
export function Alert({ tone = "info", title, children, className, ...props }: AlertProps) {
  const config = TONE_CONFIG[tone];

  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "flex gap-3 rounded-lg border px-4 py-3.5 text-sm",
        config.classes,
        className,
      )}
      {...props}
    >
      <span aria-hidden="true" className="mt-0.5 shrink-0">
        <AlertIcon tone={tone} />
      </span>
      <div className="flex flex-col gap-0.5">
        {title ? <p className="font-semibold">{title}</p> : null}
        <div className="text-ink-muted [&:first-child]:text-current">{children}</div>
      </div>
    </div>
  );
}
