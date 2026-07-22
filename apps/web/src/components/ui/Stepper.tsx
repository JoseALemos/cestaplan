import { cn } from "@/lib/utils/cn";

export interface StepperStep {
  id: string;
  label: string;
}

export interface StepperProps {
  steps: StepperStep[];
  currentStepId: string;
  className?: string;
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3 8.5l3.2 3.2L13 4.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Horizontal progress stepper for the onboarding wizard (hogar → perfil →
 * alergias → ... → comidas → presupuesto). Scrolls horizontally on small
 * screens instead of wrapping, keeping every step reachable on mobile.
 */
export function Stepper({ steps, currentStepId, className }: StepperProps) {
  const currentIndex = steps.findIndex((step) => step.id === currentStepId);

  return (
    <nav aria-label="Progreso del alta" className={cn("w-full", className)}>
      <ol className="flex items-center gap-2 overflow-x-auto pb-1">
        {steps.map((step, index) => {
          const isCompleted = index < currentIndex;
          const isCurrent = index === currentIndex;

          return (
            <li key={step.id} className="flex shrink-0 items-center gap-2">
              <div
                className={cn(
                  "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-xs font-semibold transition-colors duration-base",
                  isCompleted && "border-primary bg-primary text-primary-ink",
                  isCurrent && "border-accent bg-accent text-accent-ink",
                  !isCompleted && !isCurrent && "border-border bg-surface text-ink-faint",
                )}
                aria-current={isCurrent ? "step" : undefined}
              >
                {isCompleted ? <CheckIcon /> : index + 1}
              </div>
              <span
                className={cn(
                  "whitespace-nowrap text-sm font-medium",
                  isCurrent ? "text-ink" : "text-ink-muted",
                )}
              >
                {step.label}
              </span>
              {index < steps.length - 1 ? (
                <span aria-hidden="true" className="mx-1 h-px w-6 shrink-0 bg-border" />
              ) : null}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
