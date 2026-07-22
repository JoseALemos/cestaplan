"use client";

/**
 * Client-side favorites/rejected log (screen 17).
 *
 * The API exposes write-only actions for this (`POST/DELETE .../favorite`,
 * `POST .../feedback`) but no `GET` to list a household's favorited or
 * rejected recipes — so there is nothing to fetch for screen 17 today. This
 * keeps a local mirror of every favorite/reject action the user performs
 * anywhere in the app (plan view, recipe detail) so the screen has something
 * real to show, while the mutations themselves always go through the API
 * first (see `useFavoriteRecipeMutation` / `useRecipeFeedbackMutation`).
 */

export type FeedbackLogStatus = "favorite" | "rejected";

export interface FeedbackLogEntry {
  recipeId: string;
  title: string;
  householdId: string;
  status: FeedbackLogStatus;
  updatedAt: string;
}

const STORAGE_KEY = "cestaplan_feedback_log_v1";

function readLog(): Record<string, FeedbackLogEntry> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, FeedbackLogEntry>) : {};
  } catch {
    return {};
  }
}

function writeLog(log: Record<string, FeedbackLogEntry>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(log));
    window.dispatchEvent(new Event("cestaplan-feedback-log-changed"));
  } catch {
    // best-effort only
  }
}

export function getFeedbackLog(): FeedbackLogEntry[] {
  return Object.values(readLog()).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

export function getFeedbackStatus(recipeId: string): FeedbackLogStatus | null {
  return readLog()[recipeId]?.status ?? null;
}

export function setFeedbackStatus(
  recipeId: string,
  title: string,
  householdId: string,
  status: FeedbackLogStatus | null,
): void {
  const log = readLog();
  if (status === null) {
    delete log[recipeId];
  } else {
    log[recipeId] = { recipeId, title, householdId, status, updatedAt: new Date().toISOString() };
  }
  writeLog(log);
}
