"use client";

import Link from "next/link";
import { useEffect, useRef, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/Button";

const STORAGE_KEY = "cestaplan_cookie_notice_accepted";

type Listener = () => void;
const listeners = new Set<Listener>();
// Respaldo en memoria para la sesión actual si localStorage no está disponible
// (modo privado, almacenamiento deshabilitado, etc.).
let sessionAccepted = false;

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): boolean {
  if (sessionAccepted) return true;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function getServerSnapshot(): boolean {
  return false;
}

function acceptCookieNotice(): void {
  sessionAccepted = true;
  try {
    window.localStorage.setItem(STORAGE_KEY, "true");
  } catch {
    // Sin almacenamiento persistente disponible: se oculta solo para esta sesión.
  }
  for (const listener of listeners) listener();
}

/**
 * Aviso informativo de cookies esenciales (no es un gestor de consentimiento):
 * CestaPlan solo usa la cookie de sesión/CSRF imprescindible para funcionar,
 * así que este banner únicamente informa y no bloquea ninguna cookie.
 */
export function CookieNotice() {
  const accepted = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const ref = useRef<HTMLDivElement>(null);

  // Publish the banner's height so bottom-anchored toasts can sit ABOVE it instead of
  // overlapping. Cleared when the banner is gone so toasts drop back to the edge.
  useEffect(() => {
    const root = document.documentElement;
    if (accepted || !ref.current) {
      root.style.setProperty("--cookie-notice-height", "0px");
      return;
    }
    root.style.setProperty("--cookie-notice-height", `${ref.current.offsetHeight}px`);
    return () => root.style.setProperty("--cookie-notice-height", "0px");
  }, [accepted]);

  if (accepted) return null;

  return (
    <div
      ref={ref}
      className="fixed inset-x-0 bottom-0 z-50 border-t border-border bg-surface px-4 pt-4 shadow-lg sm:px-6"
      style={{ paddingBottom: "calc(1rem + env(safe-area-inset-bottom, 0px))" }}
    >
      <div className="mx-auto flex max-w-6xl flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm text-ink-muted">
          Usamos únicamente cookies esenciales para el funcionamiento de CestaPlan (como
          mantener tu sesión iniciada). Consulta nuestra{" "}
          <Link href="/privacidad" className="font-medium text-primary hover:underline">
            Política de Privacidad
          </Link>
          .
        </p>
        <Button size="sm" onClick={acceptCookieNotice} className="shrink-0">
          Aceptar
        </Button>
      </div>
    </div>
  );
}
