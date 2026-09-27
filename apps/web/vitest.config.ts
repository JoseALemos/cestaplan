import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest cubre SOLO los tests de COMPONENTES React (*.test.tsx) en jsdom (T3). Los tests de
// funciones puras (*.test.ts) siguen corriendo con `node --test` (ver el script `test`).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.tsx"],
    css: false,
  },
});
