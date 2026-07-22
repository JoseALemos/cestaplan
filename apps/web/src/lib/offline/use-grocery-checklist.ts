"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { toggleGroceryItem } from "@/lib/api/endpoints";
import {
  enqueueToggle,
  getAllItemStates,
  getQueuedToggles,
  removeQueuedToggle,
  setItemState,
} from "@/lib/offline/grocery-db";
import { useOnlineStatus } from "@/lib/offline/use-online-status";

export interface GroceryChecklistSync {
  isOnline: boolean;
  isHydrated: boolean;
  pendingCount: number;
  /** Resolves the checked state to render: local override if present, otherwise the server value. */
  effectiveChecked: (itemId: string, serverChecked: boolean) => boolean;
  toggle: (itemId: string, serverChecked: boolean) => Promise<void>;
}

/**
 * Bridges the grocery-list checklist (screen 15) with IndexedDB so checking
 * items works fully offline, and queues + replays toggles once the device
 * reconnects. See `lib/offline/grocery-db.ts` for the replay-log rationale.
 */
export function useGroceryChecklistSync(
  mealPlanId: string,
  onSynced?: () => void,
): GroceryChecklistSync {
  const isOnline = useOnlineStatus();
  const [overrides, setOverrides] = useState<Map<string, boolean>>(new Map());
  const [pendingCount, setPendingCount] = useState(0);
  // Hydration is derived (not duplicated state): we've hydrated for the
  // *current* mealPlanId once it matches the last plan we finished loading.
  const [hydratedMealPlanId, setHydratedMealPlanId] = useState<string | null>(null);
  const isHydrated = hydratedMealPlanId === mealPlanId;
  const syncingRef = useRef(false);
  const onSyncedRef = useRef(onSynced);

  useEffect(() => {
    onSyncedRef.current = onSynced;
  });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [states, queued] = await Promise.all([
        getAllItemStates(mealPlanId),
        getQueuedToggles(mealPlanId),
      ]);
      if (cancelled) return;
      setOverrides(states);
      setPendingCount(queued.length);
      setHydratedMealPlanId(mealPlanId);
    })();
    return () => {
      cancelled = true;
    };
  }, [mealPlanId]);

  const flush = useCallback(async () => {
    if (syncingRef.current) return;
    syncingRef.current = true;
    let syncedAny = false;
    try {
      const queued = await getQueuedToggles(mealPlanId);
      for (const entry of queued) {
        try {
          await toggleGroceryItem(entry.mealPlanId, entry.itemId);
          await removeQueuedToggle(entry.id);
          syncedAny = true;
        } catch {
          break; // network dropped again mid-flush — stop, retry on next reconnect
        }
      }
      const remaining = await getQueuedToggles(mealPlanId);
      setPendingCount(remaining.length);
    } finally {
      syncingRef.current = false;
      if (syncedAny) onSyncedRef.current?.();
    }
  }, [mealPlanId]);

  useEffect(() => {
    if (isOnline) void flush();
  }, [isOnline, flush]);

  const toggle = useCallback(
    async (itemId: string, serverChecked: boolean) => {
      const effective = overrides.has(itemId) ? (overrides.get(itemId) as boolean) : serverChecked;
      const next = !effective;
      setOverrides((prev) => new Map(prev).set(itemId, next));
      await setItemState(mealPlanId, itemId, next);

      if (isOnline) {
        try {
          await toggleGroceryItem(mealPlanId, itemId);
          onSyncedRef.current?.();
          return;
        } catch {
          // fell offline mid-flight or the request failed — queue for later.
        }
      }
      await enqueueToggle(mealPlanId, itemId);
      setPendingCount((count) => count + 1);
    },
    [mealPlanId, overrides, isOnline],
  );

  const effectiveChecked = useCallback(
    (itemId: string, serverChecked: boolean) =>
      overrides.has(itemId) ? (overrides.get(itemId) as boolean) : serverChecked,
    [overrides],
  );

  return { isOnline, isHydrated, pendingCount, effectiveChecked, toggle };
}
