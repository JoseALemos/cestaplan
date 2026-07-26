# Price-observation persistence: staging writers & concurrency guarantees

This document describes how the two-layer price model (`PriceObservation` = the economic fact,
`PriceObservationOccurrence` = each occasion a provider/crawl/parser confirmed it) is written safely
under concurrency, and which code paths are allowed to write it.

## Writers and their classification

Every place that constructs a `PriceObservation` or `PriceObservationOccurrence` is classified, and
an AST guard test (`tests/ingestion/test_writer_architecture_guard.py`) fails the build if a new,
unlisted writer appears.

| Writer | Class | Rule |
| --- | --- | --- |
| `services/observation_persistence.record_price_fact` | **canonical** | The one serialized entry point. All staging writes go through it. |
| `services/provider_sync` (staging branch) | staging | Builds a candidate and calls `record_price_fact`. |
| `services/targeted_discovery._persist_product` | staging | Builds a candidate and calls `record_price_fact` (ingests once per unique product). |
| `services/provider_sync` (production branch) | production append-only | Deliberate append-only current-price history — out of scope for the concurrency work. |
| `ingestion/price_history.record_observation` | production append-only | The FASE-A pipeline recorder (orchestration, manual entry). |
| `ingestion/licensed_catalog._append_observation` | production append-only | Licensed-catalog import. |
| `tools/dedup_staging_observations` | recovery | Exact-restore reconstruction / occurrence relink, run under controlled admin conditions. |
| `tools/backfill_observation_occurrences` | one-time additive | Backfills one occurrence per historical observation; serial and controlled. |

**Invariant:** any *staging* observation MUST be written through `record_price_fact`. A bare
`db.add(PriceObservation(...))` for a staging row is a bug the guard test is designed to catch.

## Concurrency: transactional advisory locks on the history lane

A per-*fact* lock is not enough: two DIFFERENT facts on the same product line (e.g. 1.19 € then
1.29 €) have different fact fingerprints, so they would run in parallel, both close the same prior
open row, and both insert a new `valid_until=NULL` row — **two open rows** for one line. The unit of
serialization must be the **history lane**, not the fact.

`record_price_fact` therefore takes two `pg_advisory_xact_lock`s in a **fixed order to avoid
deadlocks**:

1. **History-lane lock** — key from `price_history_lane_fingerprint` (the shared `LANE_FIELDS`).
   Acquired first. Under it, the exact fact is re-searched (reused if present) and, if new, placed
   into the lane's interval chain before insertion. The lane lock **replaces** a separate fact lock:
   two identical facts necessarily share a lane, so it already serializes their search/create.
2. **Occurrence lock** — key from the occurrence fingerprint (includes the now-known
   `price_observation_id`). Acquired second; the occurrence is re-searched and created only if absent.

No writer ever takes the occurrence lock before the lane lock, so the fixed order cannot deadlock.
Both release at `COMMIT`/`ROLLBACK`. No decisive lane read happens before the lane lock is held.

### Coherent temporal intervals (predecessor / successor)

Under the lane lock, a new fact at `observed_at = T` is placed by its true neighbours, not merely by
"the currently open row":

- **predecessor** = active row with the greatest `valid_from <= T`;
- **successor** = active row with the least `valid_from > T`;
- `candidate.valid_from = T`; `candidate.valid_until = successor.valid_from` (or `NULL` if none);
- `predecessor.valid_until = T`.

So an **out-of-order** arrival slots *between* its neighbours instead of opening a second current
row, and there is at most one open (`valid_until IS NULL`) active row per lane.

### Same-timestamp policy (two distinct facts at the same instant)

If a DIFFERENT fact already sits at exactly `T` in the lane, that is a genuine conflict — which price
was really current at `T`? Policy (chosen, documented, deterministic): **history keeps both facts**,
but every same-`T` fact (the newcomer and any still-active sibling) is marked `disputed` with an
empty `[T, T]` interval — never a "current" price — and flagged with a `same_timestamp_conflict`
`PriceAnomaly`. This is independent of arrival order and never silently picks a row by id or by who
arrived first; the current-price projection is blocked rather than arbitrary.

### Disputed timestamps are temporal barriers

A `disputed` row is excluded from the active price chain, **but it is not ignored** when building the
timeline — it is a **barrier**. Placement is **anchor-based**: an *anchor* is any non-rolled-back
row's timestamp (active or disputed). A new fact at `T` uses the nearest anchors, so:

- its `valid_until` is the next anchor — if that anchor is disputed, the fact ends exactly **on** the
  barrier and never spans it;
- the predecessor is extended to `T` **only** when the immediate previous anchor is a non-disputed
  active row; if the previous anchor is a barrier, no earlier interval is extended and a **blocked
  gap** remains from the conflict up to `T`.

A disputed timestamp therefore never becomes the current price, never acts as an extensible
predecessor, but **does** bound the neighbouring intervals: it caps the end of an earlier row and
forbids any active interval from crossing it. Concretely, for every disputed timestamp `D` no active
row may satisfy `valid_from < D AND (valid_until IS NULL OR valid_until > D)` — an active interval may
end exactly at `D`, and a new one may start after `D`, but none may cross it. The
`lane_invariant_report` counts `active_intervals_crossing_disputed` (must be 0) and `blocked_gap_count`
(barriers that correctly leave a gap); `lane_invariants_hold` fails on any crossing.

### Current-price projection blocks on gaps

New-model (staging) current-price selection (`CurrentPriceService.current(..., staging=True)`) filters
on `verification_status != 'disputed'` (not merely on `valid_until`) **and** is interval-aware: the
current price is the row whose validity CONTAINS `as_of`. Inside a conflict's blocked gap there is no
such row, so there is **no** current price — the selector does not fall back to the prior, already
closed row. Production append-only selection is unchanged. A disputed row (empty `[T, T]`) can never
be selected, costed, promoted or used by shadow planning.

### Lock-key derivation

Keys are deterministic **signed 64-bit** integers taken from the SHA-256 fingerprint bytes
(`observation_identity.signed_bigint`). Python's built-in `hash()` is **never** used — its
per-process salt would make two writers compute different keys and defeat the lock.

### Timeout and diagnostics

Each transaction runs `SET LOCAL lock_timeout`; a stuck writer therefore raises `lock_not_available`
instead of waiting forever. `record_price_fact` returns sanitized `LockDiagnostics`
(`lane_lock_acquired`, `lane_lock_wait_ms`, `occurrence_lock_acquired`, `lock_wait_ms`,
`fact_reused_after_lane_lock`, `occurrence_reused_after_lock`, `temporal_predecessor_found`,
`temporal_successor_found`, `out_of_order_insert`, `same_timestamp_conflict`) and the run-level
`RecordMetrics` aggregate them. Diagnostics contain only lock keys (non-reversible hashes) and
timings — never payloads, prices, URLs or secrets.

### History-lane invariants & read-only auditor

`services/price_history_lane.lane_invariant_report` / `lane_invariants_hold` check, per lane: at most
one open row, no overlapping intervals, `valid_from < valid_until` when not null, no repeated
`valid_from`, and disputed rows empty. `tools/audit_price_history_lanes` is a strictly **read-only**
CLI that runs the checker over existing data (optionally by provider/staging) and prints only counts
— for quantifying anomalies in production before any future change.

## Occurrence identity & NULL semantics

The occurrence identity (`observation_identity.OCCURRENCE_IDENTITY_FIELDS`) is:

`price_observation_id, provider_code, source_id, crawl_run_id, raw_capture_id, connector_version,
parser_version`

`imported_at` is deliberately **not** part of it (it is when *we* recorded the occurrence).

**NULL semantics:** a missing field equals another missing field. The fingerprint serializes `None`
to a single canonical token, so two occurrences with the same non-null values **and** the same NULLs
are the SAME occurrence (reused). A different crawl/parser/capture/source yields a different
fingerprint and therefore a new occurrence. This matters because ~204 historical Carrefour
occurrences have ambiguous (all-NULL) provenance and must still compare equal to one another.

## Why there is no UNIQUE index (yet)

A blind `UNIQUE` constraint on the occurrence identity is intentionally **not** added in this change:

- historical duplicate *facts* exist (49 groups / 145 rows for Carrefour) and are kept as evidence;
- several identity fields are nullable, and standard SQL treats `NULL`s as distinct — the opposite of
  our intended semantics;
- ~204 occurrences carry ambiguous, all-NULL provenance;
- `NULLS NOT DISTINCT` (PostgreSQL 15+) changes uniqueness semantics and must be analyzed on its own.

The correctness guarantee in this change is the **transactional serialization** above.

### Optional future migration (not implemented here)

A later, separately-reviewed migration could add a persistent `fact_fingerprint` /
`occurrence_fingerprint` column (NULL-safe, computed from the shared identity) plus a `UNIQUE` index
as defence-in-depth. It must be designed only after resolving the NULL semantics and reconciling the
historical duplicate facts — never added blindly.

## Preexisting-lane preflight & rolled-back exclusion

**The lane lock guarantees consistency only when the incoming lane is valid.** Under the lane lock,
before any write, `record_price_fact` runs a preflight over the lane's existing non-rolled-back rows
(`_preexisting_lane_anomaly`). If the lane is **already** temporally corrupt — more than one open
active row, overlapping active intervals, a repeated active timestamp with no disputed policy, a
non-positive non-disputed interval, an active interval crossing a disputed barrier, or a disputed row
with a non-empty interval — it raises `PreexistingHistoryLaneAnomaly` and makes **no** change: no
fact, no occurrence, no `valid_until` edit, no flag. The transaction leaves no partial write.

- This write path **never auto-repairs** old history. An invalid historical lane is *blocked* and
  needs a separate, reviewed remediation.
- This PR **prevents new anomalies** on valid lanes; it does not silently rewrite legacy history.
- A lane with only correct disputed barriers / blocked gaps is **not** an anomaly.
- In `provider_sync`, a blocked lane surfaces as a **quality rejection** (`blocked_lane_anomalies`
  and a `lane_anomaly_blocked_N` reason) — never an empty catalogue. Sanitized metrics only
  (`lane_preflight_checked`, `lane_preexisting_*`, `write_blocked_by_lane_anomaly`), no commercial
  data.

**Rolled-back is never a current price.** By model contract (§T) a row with `rolled_back_at` is
ignored by current-price selection. The current/latest selectors (`CurrentPriceService`,
`coverage._VariantPrice`, `readiness` open-observation count, and `project_current_prices` via
`_latest_valid`) filter `rolled_back_at IS NULL` for **both** staging and production — the exclusion
is on `rolled_back_at`, not on `valid_until` / `verification_status` alone. A rolled-back row can
never be current/latest, selectable, costable, shadow-usable, promotable or projected to
`ProductPrice`, even when `as_of` falls inside its interval.

## Writer contract v2 — a rolled-back fact is never reused

`record_price_fact` will **never reuse a rolled-back `PriceObservation`** as the existing economic
fact. Under the lane lock, `_find_existing_fact` loads the exact-fingerprint matches and partitions
them by rollback state (the SQL prefilter is only an optimization; the full 16-field fingerprint is
the source of truth). It never returns `rows[0]`/`first()`:

- **A. exactly one active (non-rolled-back) exact fact** → reuse it; the occurrence attaches to that
  fact.
- **B. only rolled-back exact facts (or none)** → reuse nothing: create a new **active** fact and
  attach the occurrence to it; every rolled-back row is preserved, immutable, and receives no new
  occurrence.
- **C. two or more active exact facts** → fail **closed** with `MultipleActiveExactFacts` and make
  zero partial writes; the writer never chooses one by id / query order / `imported_at`. (The lane
  preflight normally blocks such a lane earlier; this is an explicit last-line defence.)

A candidate that arrives already rolled-back, or already persisted/associated with a database
identity, is rejected up front with `InvalidPriceFactCandidateState` — the field is never
auto-cleared, so recording can never resurrect or accidentally reactivate a rolled-back row.

The guarantees are declared, versioned and read-only via
`RECORD_PRICE_FACT_WRITER_CONTRACT_VERSION` (`"record-price-fact-v2-active-only"`) and
`writer_contract()`:

| key | value |
| --- | --- |
| `exact_fact_reuse_requires_rolled_back_at_null` | `true` |
| `rolled_back_fact_never_receives_new_occurrence` | `true` |
| `lane_lock_required` | `true` |
| `occurrence_lock_required` | `true` |
| `active_exact_ambiguity_policy` | `fail_closed` |
| `fresh_transient_candidate_required` | `true` |
| `candidate_primary_key_must_be_null` | `true` |
| `candidate_session_must_be_null` | `true` |
| `invalid_candidate_rejected_before_sql` | `true` |

A candidate is accepted only when it is genuinely new: `state.transient` and **none** of
`pending` / `persistent` / `detached` / `deleted`, with `session is None` and `id is None`. Any other
ORM state is rejected up front — before `SET LOCAL lock_timeout`, the advisory locks, the lane read or
any autoflush (**zero SQL**) — with a stable reason code (`candidate_rolled_back`,
`candidate_primary_key_set`, `candidate_pending`, `candidate_persistent`, `candidate_detached`,
`candidate_deleted`, `candidate_session_associated`, `candidate_not_transient`). The id is never
cleared and no `expunge`/`merge` silently coerces the object to transient.

This contract is **evidence for a future, separately-reviewed apply tool**. Declaring it does **not**
make the plan-only history-lane remediation planner apply-ready; that planner stays plan-only and
blocked. Sanitized metrics only: `rolled_back_exact_matches_ignored`, `active_exact_fact_reused`,
`new_fact_created_after_rolled_back_match`, `multiple_active_exact_fact_blocked`,
`invalid_candidate_state_blocked` — counts and booleans, never a product/price/URL/fingerprint.
