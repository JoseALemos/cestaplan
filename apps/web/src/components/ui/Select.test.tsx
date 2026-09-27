import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Select } from "@/components/ui/Select";

describe("Select", () => {
  it("renderiza la etiqueta y las opciones", () => {
    render(
      <Select
        label="Moneda"
        options={[
          { value: "EUR", label: "Euro (EUR)" },
          { value: "USD", label: "Dólar (USD)" },
        ]}
      />,
    );
    expect(screen.getByRole("combobox", { name: /moneda/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Euro (EUR)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Dólar (USD)" })).toBeInTheDocument();
  });

  it("muestra el error y marca aria-invalid", () => {
    render(
      <Select
        label="Moneda"
        error="Moneda no soportada"
        options={[{ value: "EUR", label: "Euro (EUR)" }]}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Moneda no soportada");
    expect(screen.getByRole("combobox", { name: /moneda/i })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });

  it("propaga onChange al seleccionar una opción", async () => {
    const onChange = vi.fn();
    render(
      <Select
        label="Moneda"
        onChange={onChange}
        options={[
          { value: "EUR", label: "Euro (EUR)" },
          { value: "USD", label: "Dólar (USD)" },
        ]}
      />,
    );
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /moneda/i }), "USD");
    expect(onChange).toHaveBeenCalled();
  });
});
