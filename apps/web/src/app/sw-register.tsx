"use client";

import { useEffect } from "react";

/**
 * Registers the offline app-shell service worker.
 *
 * Opt-in to production only: in `next dev`, the SW would otherwise cache
 * hot-reloaded chunks and produce confusing stale-bundle bugs. This keeps
 * `next build` / `next dev` reliable even while the SW file evolves.
 */
export function ServiceWorkerRegister() {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") return;
    if (typeof window === "undefined" || !("serviceWorker" in navigator)) return;

    navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .catch((error: unknown) => {
        console.error("No se pudo registrar el service worker de CestaPlan", error);
      });
  }, []);

  return null;
}
