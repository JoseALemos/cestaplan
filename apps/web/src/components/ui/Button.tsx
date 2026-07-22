import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";

import { cn } from "@/lib/utils/cn";

const VARIANT_CLASSES = {
  primary:
    "bg-accent text-accent-ink hover:bg-accent-strong active:bg-accent-strong shadow-sm hover:shadow-md",
  secondary:
    "bg-primary-soft text-primary hover:bg-primary hover:text-primary-ink border border-transparent",
  outline: "border border-border-strong text-ink bg-transparent hover:bg-bg-subtle",
  ghost: "text-ink-muted hover:text-ink hover:bg-bg-subtle",
  danger: "bg-error text-white hover:brightness-95 shadow-sm",
} as const;

const SIZE_CLASSES = {
  sm: "h-9 px-3.5 text-sm gap-1.5",
  md: "h-11 px-5 text-[0.95rem] gap-2",
  lg: "h-13 px-6 text-base gap-2.5",
} as const;

export type ButtonVariant = keyof typeof VARIANT_CLASSES;
export type ButtonSize = keyof typeof SIZE_CLASSES;

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
  ref?: Ref<HTMLButtonElement>;
}

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  disabled,
  leadingIcon,
  trailingIcon,
  className,
  children,
  ref,
  ...props
}: ButtonProps) {
  return (
    <button
      ref={ref}
      disabled={disabled ?? loading}
      aria-busy={loading || undefined}
      className={cn(
        "inline-flex select-none items-center justify-center rounded-lg font-medium",
        "transition-[background-color,color,box-shadow,transform] duration-fast ease-plan",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-focus-ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-bg",
        "disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none",
        "active:scale-[0.98]",
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...props}
    >
      {loading ? (
        <span
          aria-hidden="true"
          className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      ) : (
        leadingIcon
      )}
      <span>{children}</span>
      {!loading ? trailingIcon : null}
    </button>
  );
}
