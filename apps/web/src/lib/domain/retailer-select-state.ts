/**
 * Single render state for a chain (retailer) selector, derived from its query flags.
 *
 * The empty state ("no chains registered") must appear ONLY when the retailers query has
 * actually SUCCEEDED with zero rows — never as the fall-through for a query that simply has no
 * data yet. React Query reports a still-`pending` query whose `fetchStatus` is `idle`
 * (offline, paused, or not yet started) with `isLoading === false` and `isError === false` and
 * `data === undefined`. A bare `options.length === 0` check renders that connectivity/timing
 * state as "todavía no hay cadenas", masking a not-loaded catalogue as an empty one. Gating the
 * empty branch on `isSuccess` closes that hole: anything that is neither an error nor a success
 * is still "loading", so a populated catalogue is never shown as empty.
 */
export type RetailerSelectState = "loading" | "error" | "empty" | "ready";

export interface RetailerQueryFlags {
  /** The query resolved successfully (`data` is populated, even if it is an empty list). */
  isSuccess: boolean;
  /** The query failed (network error, non-2xx, etc.). */
  isError: boolean;
  /** Number of selectable chain options produced from the response. */
  optionCount: number;
}

export function retailerSelectState({
  isSuccess,
  isError,
  optionCount,
}: RetailerQueryFlags): RetailerSelectState {
  // Error wins over any stale success snapshot so a failed refetch is never shown as "empty".
  if (isError) return "error";
  // Not-yet-succeeded (pending / idle / paused / fetching) is loading — never a false empty.
  if (!isSuccess) return "loading";
  return optionCount === 0 ? "empty" : "ready";
}
