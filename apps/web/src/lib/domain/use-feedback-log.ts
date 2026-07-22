"use client";

import { useEffect, useState } from "react";

import { type FeedbackLogEntry, getFeedbackLog } from "./feedback-log";

/** Reactive view over the local favorites/rejected log — updates when any tab mutates it. */
export function useFeedbackLog(): FeedbackLogEntry[] {
  const [entries, setEntries] = useState<FeedbackLogEntry[]>([]);

  useEffect(() => {
    const refresh = () => setEntries(getFeedbackLog());
    refresh();
    window.addEventListener("cestaplan-feedback-log-changed", refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener("cestaplan-feedback-log-changed", refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  return entries;
}
