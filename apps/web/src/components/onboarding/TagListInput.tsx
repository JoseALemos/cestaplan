"use client";

import { useState } from "react";

import { cn } from "@/lib/utils/cn";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export interface TagListInputProps {
  label: string;
  hint?: string;
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  className?: string;
}

/** Free-text "add a tag, see the chips, remove one" control used for intolerances / rejected ingredients. */
export function TagListInput({
  label,
  hint,
  values,
  onChange,
  placeholder,
  className,
}: TagListInputProps) {
  const [draft, setDraft] = useState("");

  const commit = () => {
    const value = draft.trim();
    if (!value) return;
    if (!values.some((existing) => existing.toLowerCase() === value.toLowerCase())) {
      onChange([...values, value]);
    }
    setDraft("");
  };

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex items-end gap-2">
        <Input
          label={label}
          hint={hint}
          value={draft}
          placeholder={placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            }
          }}
          className="flex-1"
        />
        <Button type="button" variant="secondary" size="md" onClick={commit}>
          Añadir
        </Button>
      </div>
      {values.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {values.map((value) => (
            <Badge key={value} tone="neutral" className="gap-1.5">
              {value}
              <button
                type="button"
                onClick={() => onChange(values.filter((existing) => existing !== value))}
                aria-label={`Quitar ${value}`}
                className="ml-0.5 text-ink-faint hover:text-ink"
              >
                ×
              </button>
            </Badge>
          ))}
        </div>
      ) : null}
    </div>
  );
}
