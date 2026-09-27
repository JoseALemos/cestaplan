// Matchers de jest-dom (toBeInTheDocument, toHaveValue…) sobre el `expect` de Vitest.
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Con globals desactivados, la auto-limpieza de RTL no se engancha sola: la registramos aquí para
// desmontar el DOM entre tests y evitar "multiple elements found".
afterEach(() => {
  cleanup();
});
