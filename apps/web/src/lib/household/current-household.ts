"use client";

import { useCallback, useSyncExternalStore } from "react";

const STORAGE_KEY = "cestaplan_current_household_id";
const listeners = new Set<() => void>();

function emitChange(): void {
  for (const listener of listeners) listener();
}

function subscribe(callback: () => void): () => void {
  listeners.add(callback);
  window.addEventListener("storage", callback);
  return () => {
    listeners.delete(callback);
    window.removeEventListener("storage", callback);
  };
}

function getSnapshot(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function getServerSnapshot(): string | null {
  return null;
}

/**
 * Persists which household the user is currently acting on. The API scopes
 * plans/recipes/favorites to a household but doesn't expose a "current
 * household" concept itself, so the frontend tracks it client-side (set on
 * household creation in onboarding, or on manual selection in `/households`),
 * backed by `localStorage` via `useSyncExternalStore` so every consumer
 * (including other tabs, via the `storage` event) stays in sync.
 */
export function useCurrentHouseholdId(): [string | null, (id: string | null) => void] {
  const householdId = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const setHouseholdId = useCallback((id: string | null) => {
    try {
      if (id) {
        window.localStorage.setItem(STORAGE_KEY, id);
      } else {
        window.localStorage.removeItem(STORAGE_KEY);
      }
    } catch {
      // localStorage unavailable (private mode, quota) — in-memory listeners still update this tab.
    }
    emitChange();
  }, []);

  return [householdId, setHouseholdId];
}
