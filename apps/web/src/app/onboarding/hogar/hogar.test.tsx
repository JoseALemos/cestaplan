import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

import HogarPage from "@/app/onboarding/hogar/page";
import { OnboardingProvider } from "@/lib/onboarding/onboarding-context";

function renderPage() {
  return render(
    <OnboardingProvider>
      <HogarPage />
    </OnboardingProvider>,
  );
}

describe("Onboarding · Hogar", () => {
  beforeEach(() => push.mockClear());

  it("la moneda es un desplegable con divisas soportadas, no texto libre (F5)", () => {
    renderPage();
    const moneda = screen.getByRole("combobox", { name: /moneda/i });
    expect(moneda.tagName).toBe("SELECT");
    expect(screen.getByRole("option", { name: "Euro (EUR)" })).toBeInTheDocument();
  });

  it("no avanza si falta el nombre del hogar (validación)", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Continuar" }));
    expect(await screen.findByText("Dale un nombre a tu hogar")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});
