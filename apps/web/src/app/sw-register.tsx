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
    if (typeof window === "undefined" || !("serviceWorker" in navigator)) return;

    // In development, actively tear down any service worker left over from a
    // previous production build and drop its caches. A cache-first shell that
    // outlives its build serves stale HTML pointing at hashed chunks that no
    // longer exist, which breaks pages ("This page couldn't load"). Dev must
    // never be served by a SW.
    if (process.env.NODE_ENV !== "production") {
      navigator.serviceWorker.getRegistrations().then((registrations) => {
        for (const registration of registrations) void registration.unregister();
      });
      if ("caches" in window) {
        caches.keys().then((keys) => {
          for (const key of keys) void caches.delete(key);
        });
      }
      return;
    }

    navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .catch((error: unknown) => {
        console.error("No se pudo registrar el service worker de CestaPlan", error);
      });
  }, []);

  return null;
}
