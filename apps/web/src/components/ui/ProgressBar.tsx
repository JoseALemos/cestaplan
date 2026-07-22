import { cn } from "@/lib/utils/cn";

export interface ProgressBarProps {
  value: number;
  max?: number;
  label?: string;
  showValueLabel?: boolean;
  tone?: "accent" | "primary" | "success";
  className?: string;
}

const TONE_CLASSES = {
  accent: "bg-accent",
  primary: "bg-primary",
  success: "bg-success",
} as const;

export function ProgressBar({
  value,
  max = 100,
  label,
  showValueLabel = true,
  tone = "accent",
  className,
}: ProgressBarProps) {
  const clamped = Math.min(Math.max(value, 0), max);
  const percentage = max === 0 ? 0 : Math.round((clamped / max) * 100);

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {(label || showValueLabel) && (
        <div className="flex items-center justify-between text-xs font-medium text-ink-muted">
          {label ? <span>{label}</span> : <span />}
          {showValueLabel ? <span>{percentage}%</span> : null}
        </div>
      )}
      <div
        role="progressbar"
        aria-valuenow={clamped}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-label={label}
        className="h-2.5 w-full overflow-hidden rounded-full bg-bg-subtle"
      >
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-slow ease-plan",
            TONE_CLASSES[tone],
          )}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}
