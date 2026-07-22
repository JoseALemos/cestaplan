"use client";

import { useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export interface AddItemFormProps {
  onAdd: (input: { generic_name: string; needed_quantity: string; unit: string }) => Promise<void>;
  disabled: boolean;
  disabledReason?: string;
}

export function AddItemForm({ onAdd, disabled, disabledReason }: AddItemFormProps) {
  const [open, setOpen] = useState(false);
  const [genericName, setGenericName] = useState("");
  const [quantity, setQuantity] = useState("");
  const [unit, setUnit] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (!open) {
    return (
      <div className="flex flex-col gap-1.5">
        <Button type="button" variant="secondary" onClick={() => setOpen(true)} disabled={disabled}>
          Añadir producto manual
        </Button>
        {disabled && disabledReason ? <p className="text-xs text-ink-faint">{disabledReason}</p> : null}
      </div>
    );
  }

  return (
    <form
      className="flex flex-col gap-3 rounded-md border border-dashed border-border-strong p-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!genericName.trim() || !quantity.trim() || !unit.trim()) return;
        setSubmitting(true);
        try {
          await onAdd({ generic_name: genericName.trim(), needed_quantity: quantity.trim(), unit: unit.trim() });
          setGenericName("");
          setQuantity("");
          setUnit("");
          setOpen(false);
        } finally {
          setSubmitting(false);
        }
      }}
    >
      {disabled ? <Alert tone="warning">{disabledReason}</Alert> : null}
      <div className="grid gap-3 sm:grid-cols-3">
        <Input
          label="Producto"
          placeholder="p. ej. Papel de horno"
          value={genericName}
          onChange={(event) => setGenericName(event.target.value)}
          className="sm:col-span-1"
        />
        <Input
          label="Cantidad"
          inputMode="decimal"
          placeholder="1"
          value={quantity}
          onChange={(event) => setQuantity(event.target.value)}
        />
        <Input
          label="Unidad"
          placeholder="ud, kg, l…"
          value={unit}
          onChange={(event) => setUnit(event.target.value)}
        />
      </div>
      <div className="flex gap-2">
        <Button type="submit" size="sm" loading={submitting} disabled={disabled}>
          Añadir a la lista
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Cancelar
        </Button>
      </div>
    </form>
  );
}
