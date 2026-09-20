/**
 * IndexedDB-backed offline store for the grocery-list checklist (screen 15).
 *
 * Design: the API's toggle endpoint is a pure flip (`POST .../toggle`, no
 * body, no "set to true/false") — there is no way to push an absolute
 * checked state. So instead of storing "the current checked value" as the
 * thing to sync, we record every toggle the user performs while offline as
 * its own queued event, in order, and replay them one-by-one against the API
 * once connectivity returns. Replaying N flips in the original order always
 * converges on the correct final state regardless of how many times an item
 * was tapped offline.
 *
 * Three object stores:
 * - `itemState`: last-known checked value per (mealPlanId, itemId) — used to
 *   render instantly, offline or online, before the network round-trip.
 * - `syncQueue`: ordered, not-yet-confirmed toggle events to replay.
 * - `listSnapshot`: the last successfully-fetched grocery list per plan, so the
 *   screen can open cold with no connectivity (in the supermarket) instead of
 *   erroring. The checklist overlay above still shows the right bought/unbought
 *   state, so a slightly stale snapshot is fine.
 */

import type { GroceryList } from "@/lib/api/types";

const DB_NAME = "cestaplan-grocery";
const DB_VERSION = 2;
const ITEM_STATE_STORE = "itemState";
const SYNC_QUEUE_STORE = "syncQueue";
const LIST_SNAPSHOT_STORE = "listSnapshot";

function compositeKey(mealPlanId: string, itemId: string): string {
  return `${mealPlanId}:${itemId}`;
}

export interface QueuedToggle {
  id: number;
  mealPlanId: string;
  itemId: string;
  queuedAt: number;
}

let dbPromise: Promise<IDBDatabase> | null = null;

function openDb(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("IndexedDB no disponible en este entorno"));
  }
  if (dbPromise) return dbPromise;

  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(ITEM_STATE_STORE)) {
        db.createObjectStore(ITEM_STATE_STORE, { keyPath: "compositeKey" });
      }
      if (!db.objectStoreNames.contains(SYNC_QUEUE_STORE)) {
        db.createObjectStore(SYNC_QUEUE_STORE, { keyPath: "id", autoIncrement: true });
      }
      if (!db.objectStoreNames.contains(LIST_SNAPSHOT_STORE)) {
        db.createObjectStore(LIST_SNAPSHOT_STORE, { keyPath: "mealPlanId" });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("No se pudo abrir IndexedDB"));
  });

  return dbPromise;
}

/** Best-effort IndexedDB availability check (private browsing, unsupported browsers, SSR). */
export function isIndexedDbAvailable(): boolean {
  return typeof window !== "undefined" && typeof indexedDB !== "undefined";
}

export async function getAllItemStates(
  mealPlanId: string,
): Promise<Map<string, boolean>> {
  if (!isIndexedDbAvailable()) return new Map();
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(ITEM_STATE_STORE, "readonly");
    const store = tx.objectStore(ITEM_STATE_STORE);
    const request = store.getAll();
    request.onsuccess = () => {
      const result = new Map<string, boolean>();
      for (const row of request.result as {
        compositeKey: string;
        mealPlanId: string;
        itemId: string;
        isChecked: boolean;
      }[]) {
        if (row.mealPlanId === mealPlanId) {
          result.set(row.itemId, row.isChecked);
        }
      }
      resolve(result);
    };
    request.onerror = () => reject(request.error);
  });
}

export async function setItemState(
  mealPlanId: string,
  itemId: string,
  isChecked: boolean,
): Promise<void> {
  if (!isIndexedDbAvailable()) return;
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(ITEM_STATE_STORE, "readwrite");
    tx.objectStore(ITEM_STATE_STORE).put({
      compositeKey: compositeKey(mealPlanId, itemId),
      mealPlanId,
      itemId,
      isChecked,
      updatedAt: Date.now(),
    });
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function enqueueToggle(mealPlanId: string, itemId: string): Promise<void> {
  if (!isIndexedDbAvailable()) return;
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SYNC_QUEUE_STORE, "readwrite");
    tx.objectStore(SYNC_QUEUE_STORE).add({ mealPlanId, itemId, queuedAt: Date.now() });
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function getQueuedToggles(mealPlanId?: string): Promise<QueuedToggle[]> {
  if (!isIndexedDbAvailable()) return [];
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SYNC_QUEUE_STORE, "readonly");
    const request = tx.objectStore(SYNC_QUEUE_STORE).getAll();
    request.onsuccess = () => {
      const all = request.result as QueuedToggle[];
      resolve(mealPlanId ? all.filter((entry) => entry.mealPlanId === mealPlanId) : all);
    };
    request.onerror = () => reject(request.error);
  });
}

export async function removeQueuedToggle(id: number): Promise<void> {
  if (!isIndexedDbAvailable()) return;
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SYNC_QUEUE_STORE, "readwrite");
    tx.objectStore(SYNC_QUEUE_STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function countQueuedToggles(mealPlanId?: string): Promise<number> {
  const queued = await getQueuedToggles(mealPlanId);
  return queued.length;
}

/** Persist the last successfully-fetched grocery list so the screen opens offline. */
export async function saveListSnapshot(
  mealPlanId: string,
  list: GroceryList,
): Promise<void> {
  if (!isIndexedDbAvailable()) return;
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(LIST_SNAPSHOT_STORE, "readwrite");
    tx.objectStore(LIST_SNAPSHOT_STORE).put({ mealPlanId, list, savedAt: Date.now() });
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/** The last saved grocery list for a plan, or ``null`` if none is cached. */
export async function getListSnapshot(mealPlanId: string): Promise<GroceryList | null> {
  if (!isIndexedDbAvailable()) return null;
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(LIST_SNAPSHOT_STORE, "readonly");
    const request = tx.objectStore(LIST_SNAPSHOT_STORE).get(mealPlanId);
    request.onsuccess = () => {
      const row = request.result as { list: GroceryList } | undefined;
      resolve(row?.list ?? null);
    };
    request.onerror = () => reject(request.error);
  });
}
