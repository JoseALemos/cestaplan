import type { InputHTMLAttributes, ReactNode, Ref } from "react";
import { useId } from "react";

import { cn } from "@/lib/utils/cn";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  hint?: string;
  error?: string;
  leadingIcon?: ReactNode;
  ref?: Ref<HTMLInputElement>;
}

export function Input({
  label,
  hint,
  error,
  leadingIcon,
  id,
  className,
  required,
  ref,
  ...props
}: InputProps) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const hintId = hint ? `${inputId}-hint` : undefined;
  const errorId = error ? `${inputId}-error` : undefined;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={inputId} className="text-sm font-medium text-ink">
        {label}
        {required ? (
          <span aria-hidden="true" className="ml-0.5 text-accent-strong">
            *
          </span>
        ) : null}
      </label>
      <div className="relative flex items-center">
        {leadingIcon ? (
          <span aria-hidden="true" className="absolute left-3.5 text-ink-faint">
            {leadingIcon}
          </span>
        ) : null}
        <input
          ref={ref}
          id={inputId}
          required={required}
          aria-invalid={Boolean(error) || undefined}
          aria-describedby={cn(hintId, errorId) || undefined}
          className={cn(
            "h-11 w-full rounded-md border border-border bg-surface px-3.5 text-[0.95rem] text-ink placeholder:text-ink-faint",
            "transition-colors duration-fast ease-plan",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-focus-ring)] focus-visible:border-transparent",
            error && "border-error focus-visible:ring-error",
            leadingIcon && "pl-10",
            className,
          )}
          {...props}
        />
      </div>
      {hint && !error ? (
        <p id={hintId} className="text-xs text-ink-muted">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} role="alert" className="text-xs font-medium text-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}
