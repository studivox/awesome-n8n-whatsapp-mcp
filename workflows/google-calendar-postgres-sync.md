# Google Calendar ↔ Postgres Synchronization

## What it does

A reusable **sub-workflow** (Execute Workflow Trigger — meant to be called on a schedule or by an orchestrator, not exposed as a webhook) that keeps one Google Calendar in sync with Postgres, in both directions, without ever letting either side silently overwrite the other:

- **Pull-sync** (Calendar → Postgres): fetches new/changed/cancelled events via `events.list`, paginated, using an incremental sync token when available and falling back to a full resync when the token expires (`410 Gone`) or none exists yet. This direction never mutates an appointment's real content — it only refreshes cached drift-detection metadata (a strict, bounded, five-field allowlist projected from each event — see [Data-minimization design](#data-minimization-design)) and raises a controlled conflict when something changed on the Calendar side that Postgres didn't originate. Every provider-controlled field is validated and bounded before it is ever cast, stored, or used to build a URL — see [Provider-response validation](#provider-response-validation).
- **Outbox drain** (Postgres → Calendar): applies pending `create`/`update`/`cancel` operations queued in `sync_outbox` by whatever mutates an appointment (this workflow does not enqueue outbox rows itself — see [Outbox and reconciliation design](#outbox-and-reconciliation-design)). Every path fails closed: a failed or malformed idempotency lookup makes **zero** `POST` calls (see [Idempotency pre-check design](#idempotency-pre-check-design)); an `update`/`cancel` with a missing, invalid, or unbounded event id or etag makes **zero** HTTP calls of any kind (see [Mutation fail-closed design](#mutation-fail-closed-design)); an outbox row carrying anything other than `create`/`update`/`cancel` is rejected before any HTTP call is even considered.

It accepts exactly one input: `syncCalendarId`, an **internal id** referencing a row in the `sync_calendars` table. **The real Google Calendar id is never a caller input** — it's read exclusively from that row, so arbitrary caller-supplied data can never redirect a Calendar API call to a calendar this deployment doesn't own. The Calendar API host is fixed (`https://www.googleapis.com/calendar/v3`).

A single row-based **lease** (not `pg_advisory_lock` — see [Feasibility investigation](#feasibility-investigation) for why) ensures only one runner touches a given calendar's pull-sync *and* outbox drain at a time, and self-heals on a fixed TTL if a runner crashes without releasing it.

## Real business use case

Appointments get created, confirmed, cancelled, and rescheduled in Postgres (for example, by [`whatsapp-appointment-confirmation-cancellation`](whatsapp-appointment-confirmation-cancellation.md)). Someone on staff also edits the calendar directly sometimes — moves a meeting, cancels one from their phone. Without a reconciliation loop, these two views of "what's actually happening" drift apart silently. This workflow is that reconciliation loop: it pushes Postgres's outbox of pending Calendar operations out, and it pulls Calendar's own state back in far enough to notice — and flag, never silently resolve — anywhere the two disagree.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2, against an isolated **PostgreSQL 16.15** instance for testing.

## Feasibility investigation

| Question | Finding |
|---|---|
| Can a Postgres-node-based lease survive n8n's connection pooling? | **Central finding.** n8n's Postgres node checks a client out of a pool per query and returns it after — it does not guarantee the same physical session across separate node calls in one execution. A row-based lease (ordinary compare-and-set `UPDATE`s) has no connection-affinity dependency and self-heals on a fixed TTL. Used the lease design. |
| Does n8n support the open-ended "keep fetching while there's a next page token" loop this design needs? | Yes — verified directly with a cyclic back-edge connection, node state correctly correlating to the *current* iteration. |
| Does a zero-row Postgres result matter for n8n's execution model here too? | Yes — `alwaysOutputData: true` on the outbox-claim node, verified experimentally, lets the next node distinguish "0 claimed" from "N claimed" instead of being silently skipped. |
| Does one HTTP Request node support per-item, expression-driven method/URL/body? | Yes — confirmed from n8n's own source (`getNodeParameter('method', itemIndex)`). |
| Does a Code node in "Run Once for Each Item" mode behave the way per-item outbox classification needs? | Two corrections found only by testing: (1) `$input.first()/.last()/.all()/.itemMatching()` are explicitly disallowed in that mode — `$json` is the correct per-item accessor. (2) The mode requires returning a bare object (`return { json: {...} }`), not an array. |
| Does `$('NodeName').first()`, referenced from inside a per-item Code node, correctly correlate to the *current* item? | **No — always the first item of whatever that node produced, not item-correlated,** and this does not trip n8n's each-item-mode validator (only bare `$input.*` is blocked), so it fails silently. Fixed by using `$('NodeName').item.json` (paired-item aware) everywhere a per-item node reaches an upstream per-item node by name. Re-verified with a genuine 2-item batch (distinct `update` + `cancel` operations, different appointments): each item ended up mapped to its own, distinct Calendar event, not cross-contaminated. |
| Does a Postgres "Execute Query" node's own output silently discard the item's other fields? | Yes — its output is exactly the query's result columns, nothing carried over from the incoming item. Two distinct bugs were caused by this and are documented precisely because they were genuinely shipped and then caught by review, not just anticipated: (1) the calendar-error and aborted terminal responses were being **silently replaced** by the `Release Lease` node's own raw `{out_released}` output, because each path's final node was the lease-release call itself — fixed by adding a `Restore ... Response` node after each release that re-reads the built response by name (`$('Build Calendar Error Response').first().json`). (2) A `RETURNS TABLE(out_ok boolean, ...)` column can be ambiguous even across nested subqueries calling *another* function with the same output column name — `sync_outbox_finalize`'s conflict/failure branches originally did `SELECT (SELECT out_ok FROM sync_outbox_finalize_conflict(...)), p_reason`, and PostgreSQL could not resolve `out_ok` between the outer function's own return-table variable and the inner function's result column, raising `column reference "out_ok" is ambiguous` and aborting the finalize call outright. Fixed by capturing the inner call's result into a local `v_ok` variable first. Both were caught only by executing the real workflow end-to-end against real PostgreSQL, not by any unit test of the SQL alone. |
| Does the mock Calendar server used for testing implement real Google incremental-sync semantics? | Initially, no — it returned the full unfiltered event set on every `syncToken`-based call regardless of what had actually changed, which silently masked a real correctness question (does this workflow correctly rely on Google's actual sync-token contract, or does it accidentally depend on the mock's over-generous behavior?). Fixed the mock to track a monotonic generation per event and only return events whose generation is newer than the token's issuance point, and to always include cancelled events in incremental results (matching Google's documented deletion-detection behavior) regardless of the `showDeleted` flag. This also revealed that `Build Fetch Params` was not requesting `showDeleted=true` for incremental syncs — fixed; a full/baseline sync intentionally still omits it (avoids pulling an unbounded history of already-cancelled events on the very first sync). |
| Can the real Google Calendar API be used for testing? | No — no dedicated synthetic test calendar/credential available. **The real Google Calendar API was never contacted.** All Calendar-dependent testing used a temporary, uncommitted mock-bound copy of this workflow, disclosed here and in [Test procedure](#test-procedure). |

**Feasibility verdict: GO**, with every design and implementation defect above corrected and re-verified before any test was counted as passing.

## Synchronization authority and conflict policy

- **Postgres is authoritative for appointment identity and for the appointment ↔ Calendar-event mapping.** The mapping (`appointment_calendar_mappings`) is only ever created two ways: (a) the outbox drain creates a Calendar event and records the mapping it just made, or (b) a pull-sync full resync adopts a *pre-existing* Calendar event for a known appointment — but **only** when that event carries a marker (`extendedProperties.private.appointmentId`) matching a real row already in `appointments`, and only when that appointment doesn't already have a different mapping. An event that isn't marked, or is marked with an appointment id Postgres doesn't recognize, is never adopted — it's recorded as a conflict for a human to resolve.
- **Never matched by name, title, phone number, or email.** The only identifier ever used to correlate a Calendar event with an appointment is the stable, client-generated `appointmentId` written into `extendedProperties.private` at creation time.
- **Never a caller-controlled Calendar event id or Calendar id.** `syncCalendarId` (the only caller input) is validated as an opaque internal identifier and used solely as a `WHERE` parameter against `sync_calendars`; the real `google_calendar_id` it resolves to, and every Calendar event id this workflow acts on, come exclusively from that row or from prior Postgres state (`appointment_calendar_mappings.google_event_id`) — never from the trigger's input or from unvalidated Calendar response data used as-is.
- **Conflicts are never silently resolved in either direction.** Distinct conflict reasons are recorded in `sync_conflicts`, each requiring a human or a separate reconciliation process to resolve — see the table in [Outbox and reconciliation design](#outbox-and-reconciliation-design):
  - `unknown_appointment_reference` — a Calendar event's marker references an appointment id Postgres has never heard of.
  - `duplicate_mapping_candidate` — a Calendar event's marker references an appointment that's already mapped to a *different* event.
  - `concurrent_edit` — a mapped event's `etag` changed on the Calendar side without a corresponding outbox push explaining it.
  - `calendar_side_cancellation` — a mapped event was cancelled directly on Calendar, not through the outbox.
  - `missing_from_calendar` — a full resync completed without ever seeing a mapped, non-cancelled event again (it vanished from Calendar's own listing).
  - `appointment_id_mismatch` — an incoming event's marker no longer matches the appointment id this workflow's own mapping recorded for that exact Calendar event id.
  - `malformed_event` — an incoming event fails bounded/allowlist validation (oversized or wrong-shaped id/etag/status/timestamp/marker) — see [Provider-response validation](#provider-response-validation).

## Cursor, locking, and pagination design

**Lease, not `pg_advisory_lock`.** See [Feasibility investigation](#feasibility-investigation) for why. `sync_calendars` carries `lease_owner`/`lease_expires_at`; acquiring is a single compare-and-set `UPDATE … WHERE lease_owner IS NULL OR lease_expires_at < now()`. A second runner attempting the same calendar while the lease is held gets `lease_busy` immediately — verified to make **zero** Calendar or further Postgres calls in that case.

**Durable per-calendar cursor**, also on `sync_calendars`: `sync_token` (Google's incremental token; `NULL` means a full resync is owed), `pending_page_token` (mid-pagination resume point), `needs_full_resync`, `full_sync_started_at` (stamped when a full-resync run begins, used to detect events that vanished from Calendar entirely — see `missing_from_calendar` above).

**Pagination**: each page is applied and the cursor advanced **in the same Postgres statement** (`sync_process_page`) — the durable state after that call is *either* "this page fully applied and the cursor points past it" *or* (on any failure inside that call, or on a page that fails validation before that call is even made — see [Provider-response validation](#provider-response-validation)) "nothing about this page happened and the cursor is exactly where it was." There is no reachable state in between. Verified directly via crash-injection and via deliberately malformed pages.

**Expired sync token (`410 Gone`)**: caught, the cursor is reset to a clean full-resync state (`sync_reset_for_full_resync`, itself lease-guarded and idempotent), and the pagination loop restarts as a full sync from the beginning.

**Incremental sync correctness**: verified against a mock server that implements real Google sync-token semantics — a monotonic generation counter per event, with incremental listings filtered to `generation > token's issuance generation`, and cancelled events always included in incremental results regardless of `showDeleted` (matching Google's documented deletion-detection behavior). `showDeleted=true` is explicitly requested on every incremental fetch; a baseline (full) sync intentionally omits it. See [Feasibility investigation](#feasibility-investigation) for how this gap was found and fixed.

## Idempotency pre-check design

Before any `create` ever calls `POST`, the workflow looks up whether an event carrying this outbox row's `idempotencyKey` already exists (`privateExtendedProperty` exact-match query against the Calendar API). The lookup's result is classified into exactly one of five decisions, computed entirely client-side (never trusting the provider's own server-side filter blindly) before any further action is taken:

| Decision | Condition | Result |
|---|---|---|
| `lookup_failed` | The lookup HTTP call did not return a genuine `200` with an object body whose `items` field is an array — covers non-2xx status (`401`/`403`/`429`/`500`/…), a transport-level failure or timeout, and a `200` with a malformed body shape | **Failure outcome. Zero `POST` calls.** |
| `no_match` | The lookup returned zero items | Proceeds to build and send the actual `create` request. |
| `single_valid_match` | The lookup returned exactly one item, and that item independently passes bounded validation (id present, bounded, allowlisted) **and** its `appointmentId` and `idempotencyKey` markers both exactly match this outbox row's own values | Adopts the existing event — **zero `POST` calls.** |
| `mismatched_match` | The lookup returned exactly one item, but it fails the bounded/marker check above | **Conflict outcome. Zero `POST` calls, zero mutation.** |
| `multiple_matches` | The lookup returned more than one item | **Conflict outcome. Zero `POST` calls, zero mutation** — regardless of whether any individual item would itself have validated. |

This is deliberately stricter than trusting Google's own `privateExtendedProperty` server-side filter: the filter is re-verified client-side (id/etag bounds, exact marker match) before an item is ever treated as a genuine match, and any ambiguity (more than one candidate, or a single candidate that doesn't actually match) fails closed into a conflict rather than guessing.

## Mutation fail-closed design

An outbox row's `operation` is strictly validated to be exactly one of `create`, `update`, or `cancel` — anything else (`route: 'invalid_operation'` internally) is rejected as a failure outcome before any HTTP call is even considered, with zero Calendar calls.

For `update` and `cancel`, before the corresponding HTTP call is ever built:

- The database-sourced Calendar event id (`appointment_calendar_mappings.google_event_id`, read at claim time) must be a genuine JavaScript string, non-empty, at most 1024 characters, and match `^[A-Za-z0-9_-]+$`. A JSON `null` (no mapping exists yet) or `undefined` fails this check by **type**, not by string comparison — there is no code path that could ever produce a request to `/events/null` or `/events/undefined`, because the value is never coerced to a string before this check runs.
- `update` additionally requires a bounded, non-empty etag (same shape check). `cancel` does not require an etag (Google's `DELETE` doesn't take a conditional precondition the same way `PATCH` does in this design).
- Any failure of the above produces a `conflict` outcome (`missing_or_invalid_event_id` or `missing_or_invalid_etag`) with **zero HTTP calls of any kind** — the `Mutate Event` node is never reached on this path; a dedicated `Mutate Request Valid?` gate routes invalid requests directly to a result-building node instead.

`update` is sent with an `If-Match: <etag>` header. A concurrent Calendar-side edit this workflow doesn't know about makes the etag stale, and Google's conditional-request semantics reject it with `412` — recorded as an explicit `conflict` outcome, never silently overwritten.

**Repeated cancellation — re-tested and corrected.** A prior draft of this documentation claimed cancellation was "naturally idempotent" because repeating a `DELETE` against an already-cancelled event was assumed to be a harmless no-op. That claim was not verified against real Google Calendar API behavior and was wrong: **Google's `events.delete` returns `410 Gone` on a second call against an already-deleted/cancelled event, not another success.** The mock server used for testing was corrected to reproduce this exactly (first `DELETE` on a confirmed event → `204`; a second `DELETE` on that same, now-cancelled event → `410`), and `Classify Mutate Result` was corrected to treat a `410` response **specifically and only for `cancel` operations** as a successful, idempotent outcome — the already-cancelled state *is* the desired end state. This was verified directly: two independent `cancel` outbox operations enqueued against the same appointment both finalized as `applied`, with the mock server's own recorded event state confirming the second `DELETE` genuinely received `410` and was still correctly classified as success. Cancellation is idempotent **because this is explicitly handled**, not because a blind repeat `DELETE` happens to return an identical result.

## Data-minimization design

`sync_process_page` never stores a raw Calendar event. Before anything reaches `sync_conflicts` or `appointment_calendar_mappings`, every event is projected into a strict, five-field allowlist:

```sql
jsonb_build_object(
  'event_id', left(v_event_id, 1024),
  'status', v_status,
  'etag', CASE WHEN v_etag IS NOT NULL THEN left(v_etag, 512) ELSE NULL END,
  'updated', v_updated,
  'appointment_id_marker', v_appt_id
)
```

Nothing else from the provider payload — no `summary`, `description`, `location`, `attendees`, attendee emails, `organizer`, `conferenceData`, or `attachments` — is ever read again after this projection is built, let alone persisted. This was verified directly, not just by code inspection: a synthetic event carrying realistic values for every one of those fields (attendee/organizer email addresses, a conference link, an attachment URL, description text) was fed through the real workflow, and every value was confirmed **absent** from every row written to Postgres afterward.

**Explicit size limits**, enforced before any per-item processing and before any Postgres cast:

| Field | Limit |
|---|---|
| Event id | 1–1024 chars, `^[A-Za-z0-9_-]+$` |
| Status | Must be exactly `confirmed`, `cancelled`, or `tentative` |
| Etag | ≤512 chars |
| `updated` timestamp | ≤64 chars, and must parse as a genuine timestamp (validated inside an exception-safe block — an unparseable value is counted as malformed, never allowed to abort the page) |
| `appointmentId` marker | 1–128 chars, `^[A-Za-z0-9_-]+$` |
| Items per page | ≤2500 (Google's own documented maximum) |
| `nextPageToken` / `nextSyncToken` | ≤2048 chars |

Any item failing these checks is counted as `malformed` and is **not** stored beyond a minimal `{reason: '...'}` marker in `sync_conflicts.details` — never the offending field's actual value, bounded or not, when the field itself is what's invalid (e.g. an oversized id is never echoed back, even truncated, into the stored conflict). Every per-item code path also runs inside a nested exception-safe block in the PL/pgSQL function: any *unexpected* error for one item (a constraint violation, a coercion failure the explicit checks above didn't anticipate) is caught and counted as malformed, and can never abort the rest of the page.

## Provider-response validation

Two independent, redundant layers validate the shape of everything the Calendar API returns, so a malformed response can never reach a Postgres cast or a URL:

1. **n8n (`Classify Fetch Result`, before `Process Page` is ever called)**: the HTTP status must be exactly `200`; the body must be a genuine JSON object (not an array, not a primitive); `items` must be an array with at most 2500 entries; `nextPageToken`/`nextSyncToken`, if present, must be non-empty strings of at most 2048 characters. **Any failure here routes to a controlled `calendar_error` response and releases the lease — `sync_process_page` is never invoked, and the cursor is provably untouched**, because the only thing that ever advances it is that function call.
2. **PostgreSQL (`sync_process_page`, defense in depth)**: re-validates that `p_items` is a genuine JSONB array, re-checks the item-count and token-length bounds, and per-item validates every field as described in [Data-minimization design](#data-minimization-design) — in case a future change to the n8n side ever bypasses layer 1.

An invalid page-level response (wrong shape, oversized token, non-array `items`) fails closed at layer 1 and never reaches Postgres at all; an individual malformed *event* within an otherwise-valid page is caught at layer 2, counted as `malformed`, and does not block the rest of the page or the cursor advance for the events that *are* valid.

## Controlled-response preservation

Every terminal path restores its intended response **after** the lease-release Postgres call, rather than ending at that call directly. This matters because a Postgres "Execute Query" node's output is exactly its query's result columns — it does not carry the incoming item's fields forward (see [Feasibility investigation](#feasibility-investigation)). A prior version of this workflow ended the `calendar_error` and `aborted` paths directly at `Release Lease`, so the actual built response (with its `httpStatus`/`reason`, or its abort `reason`) was silently discarded and replaced by the lease-release call's own `{out_released: true}` output. Fixed: `Restore Calendar Error Response` and `Restore Aborted Response` each re-read the earlier, already-built response by name after the corresponding `Release Lease` call completes. Verified directly for every terminal path:

| Path | Verified final response |
|---|---|
| `rejected` | `{ status: 'rejected', reason: 'invalid_sync_calendar_id' }` — never touches Postgres. |
| `lease_busy` | `{ status: 'lease_busy' }` — never touches Postgres beyond the failed acquire attempt. |
| `calendar_error` | `{ status: 'calendar_error', httpStatus, reason, syncCalendarId, ownerId }`, confirmed present **after** `Release Lease (Error)` runs; lease confirmed released in Postgres directly. |
| `aborted` | `{ status: 'aborted', reason, syncCalendarId, ownerId }`, same restoration pattern after `Release Lease (Aborted)`. |
| `ok` (success) | `{ status: 'ok', pull: {...}, outbox: {...} }`, built from `Accumulate Outbox Results` (referenced by name, not by the immediately-preceding `Release Lease (Success)` node's own output). |

## Outbox and reconciliation design

`sync_outbox` holds `pending` Postgres → Calendar operations; **this workflow only drains it, it does not enqueue rows** — that's the responsibility of whatever mutates an appointment (e.g. the confirmation/cancellation workflow). A drained row moves through `pending → in_flight → applied | conflict | failed`. A row stuck `in_flight` past a staleness threshold (a prior runner crashed after claiming it but before finalizing) is safely reclaimable by a later drain, because every outcome is designed to be safe to retry or to have already been made impossible to reach twice — see [Idempotency pre-check design](#idempotency-pre-check-design) and [Mutation fail-closed design](#mutation-fail-closed-design) above.

Every finalize call (`sync_outbox_finalize`) is lease-guarded the same way the pull-sync writes are, and is a single statement regardless of outcome (success/conflict/failure), so there's one Postgres write path to reason about rather than three.

| Durable state | Meaning | Resolution |
|---|---|---|
| `sync_outbox.status = 'pending'`/`'failed'` | Not yet applied, or a prior attempt failed (network, non-2xx, invalid operation, etc.) | Reclaimed automatically by the next drain |
| `sync_outbox.status = 'in_flight'` past staleness | A runner claimed it and then crashed before finalizing | Reclaimed automatically by a later drain (idempotency-checked for `create`; fail-closed-validated for `update`/`cancel`) |
| `sync_outbox.status = 'conflict'` / `appointment_calendar_mappings.sync_status = 'conflict'` | Calendar rejected the write (etag mismatch), the idempotency lookup found an ambiguous or mismatched result, the mutate request failed pre-flight validation, or a pull-sync detected a Calendar-side edit this workflow didn't originate | Requires a human (or a separate, out-of-scope reconciliation workflow) to decide which side is correct |
| `sync_conflicts` row, any `reason` | See [Synchronization authority and conflict policy](#synchronization-authority-and-conflict-policy) | Same — a human decides; this workflow never auto-resolves any of them |

**This workflow never claims atomicity across Postgres and Google Calendar** — it's structurally impossible, and every design choice above (idempotency keys, conditional requests, lease-guarded finalize, staleness-based reclaim, fail-closed validation before every mutating call) exists specifically to make the *un-atomic* gap between "Calendar call happened" and "Postgres recorded it" safe to retry or safe to leave for reconciliation, never silently wrong.

## Crash-consistency results

| Crash point | Result |
|---|---|
| Between claiming an outbox row and the Calendar call | Row remains `pending`; never claimed as `in_flight` until an actual claim happens. |
| Calendar `create` call succeeds, then Postgres becomes unreachable before finalize | Outbox row left `in_flight`, `google_event_id` still `NULL` on that row (finalize never ran) — **not** falsely marked applied. A later drain's idempotency pre-check found the already-created event and adopted it, making zero additional `POST` calls. |
| A test-only variant of the page-processing function raises immediately after applying item 1 of a 2-item page, before the cursor-advance write | The entire call rolled back: **zero** rows in `appointment_calendar_mappings` for the crashed page, and the cursor completely untouched. |
| A page-level response fails validation (invalid `items` shape, oversized token) | `sync_process_page` is never called at all — the cursor is provably untouched because nothing but that function ever advances it. |
| Postgres unreachable before a run starts | The lease-acquire call fails immediately and loudly; zero Calendar calls occur. |
| A calendar event carrying an unrecognized or mismatched marker | Never mutates `appointments` or an unrelated mapping — recorded as a conflict, mapping/appointment state left exactly as it was. |

## Test procedure

Built and verified in an isolated local n8n test environment (the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, isolated `N8N_USER_FOLDER` and SQLite database) against an isolated local **PostgreSQL 16.15** instance (fresh `initdb` cluster, non-default port, synthetic data only). **The real Google Calendar API was never contacted** — every test requiring a Calendar call used a temporary, uncommitted mock-bound copy of this workflow, never exported or committed.

Two layers of testing, both required for confidence in a design this concurrency- and validation-sensitive:

1. **Direct-SQL engine tests** (27 scenarios) — the exact orchestration logic implemented once more in a small Python harness calling the same PL/pgSQL functions and the same mock Calendar server.
2. **Real n8n workflow tests** (26 scenarios) — the actual committed graph (or its temporary mock-bound copy), executed through n8n's own REST API, inspecting Postgres directly and mock-server call counts as evidence rather than relying on the workflow's own reported output alone. This layer is what caught every n8n-execution-specific defect in this round — none of them were visible at the SQL layer.

| # | Scenario | Result | Verified via |
|---|---|---|---|
| 1 | Initial full synchronization | All pre-existing, appointment-marked events adopted; cursor set | both layers |
| 2 | Incremental synchronization | Only the genuinely new event applied, using a real, generation-filtered incremental listing | both layers |
| 3 | Multiple pages | 7/3 events across multiple pages, pagination loop ran the correct number of times | both layers |
| 4 | Empty / no-change run | Zero applied, zero errors | both layers |
| 5–7 | Outbox create / update / cancel (happy path) | Event created/updated/cancelled correctly; mapping reflects real Calendar-assigned ids/etags | both layers |
| 8–9 | Unknown event / missing marker | Flagged, never auto-adopted | both layers |
| 10 | Duplicate delivery / replayed page | Idempotent — no duplicate mappings | direct SQL |
| 11 | Out-of-order event data | Stale etag flagged as conflict, not blindly accepted | direct SQL |
| 12–13 | Concurrent runners (same / different calendars) | Exactly one succeeds on the same calendar; both succeed independently on different calendars | direct SQL |
| 14 | Postgres/Calendar edit conflict | Detected, not silently overwritten | both layers |
| 15 | Expired sync token, safe resync | Full resync restarted correctly, including an event created after invalidation | both layers |
| 16 | Google 400/401/403/404/429/500/timeout (pull-sync) | Each handled without crashing; controlled `calendar_error`; zero further mutation | direct SQL |
| 17–19 | Postgres unavailable before/during; cursor-not-advanced on crash | All fail loudly/safely with zero partial state | direct SQL (crash-injection for #19) |
| 20 | Zero unauthorized Calendar calls on lease-busy | Confirmed via mock-server call log | direct SQL + real n8n execution |
| 21 | Idempotency lookup `401`/`403`/`429`/`500`/timeout | Every case: `lookup_failed` decision, **zero `POST` calls** | real n8n execution |
| 22 | Malformed idempotency lookup response (`items` not an array) | `lookup_failed` decision, zero `POST` calls | real n8n execution |
| 23 | Zero / one / multiple idempotency matches | Zero → creates; one valid → adopts with zero `POST`s; multiple → `conflict`, zero `POST`s | real n8n execution |
| 24 | One match with a mismatched `appointmentId` | `conflict`, zero mutation, zero `POST`s | real n8n execution |
| 25 | `update` with a missing/invalid event id | `conflict`, **zero HTTP calls of any kind** | real n8n execution |
| 26 | `update` with a missing/invalid etag | `conflict`, zero HTTP calls | real n8n execution |
| 27 | Invalid outbox operation (not `create`/`update`/`cancel`) | `failure`, zero HTTP calls | real n8n execution |
| 28 | Rich provider fields (synthetic summary, description, location, attendee/organizer email, conference data, attachment) | None of those values found anywhere in Postgres afterward | real n8n execution, direct SQL inspection |
| 29 | Oversized/malformed event fields, invalid timestamps | All flagged `malformed`, zero applied, page still completes | real n8n execution |
| 30 | Invalid `items` shape, oversized page token, oversized sync token | Each fails closed with a controlled `calendar_error`; cursor confirmed unchanged (`sync_token`/`pending_page_token` still `NULL`) | real n8n execution, direct SQL inspection |
| 31 | Cursor unchanged on a rejected/invalid page (against a calendar with a pre-existing, non-null cursor) | Cursor value confirmed byte-identical before and after the rejected page | real n8n execution, direct SQL inspection |
| 32 | Calendar-error and aborted final response schemas, after lease release | Both confirmed present and correctly shaped; lease confirmed released in Postgres | real n8n execution |
| 33 | Multi-page pull-sync, `410` recovery, two-item paired-item correctness, repeated cancellation | All re-verified against the corrected graph — see [Mutation fail-closed design](#mutation-fail-closed-design) for the repeated-cancellation result specifically | real n8n execution |

All 27 direct-SQL scenarios and all 26 real-n8n-execution scenarios pass (53 total; several SQL scenarios and n8n scenarios cover the same requirement from each layer, counted individually above).

## Clean re-import results

- **Official CLI export/import**: exported via `n8n export:workflow`, sanitized (zero nodes carry a `credentials` key — verified programmatically), imported into a second, genuinely fresh n8n instance.
- **Second clean PostgreSQL instance**: a fresh `initdb` cluster (different port, different database) with the schema and all ten functions installed from scratch via the exact DDL in [Schema](#schema).
- **Execution-data persistence**: this workflow's settings disable execution persistence (`saveManualExecutions: false`, `saveDataSuccessExecution/ErrorExecution: 'none'`) — confirmed the committed file carries these settings.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; single input field, `syncCalendarId`.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope note; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used **twenty-five** times: input validation, page-state initialization and accumulation, fetch/finalize classification and response restoration, and per-item outbox classification, idempotency decision-making, and mutate-request validation (see [Feasibility investigation](#feasibility-investigation) for the two n8n-specific each-item-mode behaviors this design accounts for).
- **IF** (`n8n-nodes-base.if`, v2.2) — used **twelve** times, gating every mutation and every loop-continuation decision structurally: input validity, lease acquisition, HTTP success + page-shape validity, expired-token detection, page-apply success, final-page detection, whether there's outbox work at all, create-vs-mutate-vs-invalid-operation routing, whether the idempotency lookup found a genuinely usable match, and whether a mutate request passes its own pre-flight validation.
- **Merge** (`n8n-nodes-base.merge`, v3, append mode) — used **two** times, to reconverge the create/mutate/invalid-operation outbox branches before a shared finalize step, and to reconverge the "no outbox work" path with the finalized-work path before the final summary.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used **eight** times, `Execute Query` operation, every query fully parameterized. Each call is a single, self-contained function invocation (see [Schema](#schema)).
- **HTTP Request** (`n8n-nodes-base.httpRequest`, v4.2) — used **four** times (fetch events page, idempotency pre-check, create event, mutate event), each with `Never Error` + `Full Response` + `onError: continueErrorOutput`, so HTTP-status failures and transport-level failures both route to controlled classification instead of crashing the execution.

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature.

## Required credentials

**Two**, neither included in the exported JSON — no node has a credential bound after import, by design:

| Credential | Bound to node(s) | Type |
|---|---|---|
| e.g. "Sync Postgres" | `Acquire Lease`, `Reset For Full Resync`, `Process Page`, `Release Lease (Error)`, `Release Lease (Aborted)`, `Claim Outbox Batch`, `Finalize Outbox Item`, `Release Lease (Success)` | n8n **Postgres** credential (`postgres`), pointed at your own database with the schema and functions in [Schema](#schema) |
| e.g. "Calendar Access" | `Fetch Events Page`, `Check Existing Event`, `Create Event`, `Mutate Event` | n8n **Google Calendar OAuth2 API** credential (`googleCalendarOAuth2Api`) — set up your own Google Cloud OAuth2 app exactly as you would for n8n's native Google Calendar node |

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Schema

Full schema for a new installation — table DDL first, then the ten PL/pgSQL functions this workflow calls (verified, exact function bodies as tested — see [Test procedure](#test-procedure)):

```sql
CREATE TABLE appointments (
  appointment_id       TEXT PRIMARY KEY,
  status                TEXT NOT NULL DEFAULT 'scheduled',
  calendar_event_id     TEXT,
  version                INTEGER NOT NULL DEFAULT 1,
  calendar_sync_status   TEXT NOT NULL DEFAULT 'not_applicable',
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Already present if you have `whatsapp-appointment-confirmation-cancellation`
-- installed -- this workflow reads/writes the same appointments table, it
-- does not define a competing one. Only create it if you don't already have it.

CREATE TABLE sync_calendars (
  id                  TEXT PRIMARY KEY,
  google_calendar_id  TEXT NOT NULL,
  sync_token          TEXT,
  pending_page_token   TEXT,
  needs_full_resync    BOOLEAN NOT NULL DEFAULT true,
  last_full_sync_at     TIMESTAMPTZ,
  last_synced_at         TIMESTAMPTZ,
  lease_owner             TEXT,
  lease_expires_at        TIMESTAMPTZ,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  full_sync_started_at     TIMESTAMPTZ
);

CREATE TABLE appointment_calendar_mappings (
  appointment_id        TEXT PRIMARY KEY,
  calendar_id           TEXT NOT NULL REFERENCES sync_calendars(id),
  google_event_id       TEXT NOT NULL,
  etag                  TEXT,
  mapping_version       INTEGER NOT NULL DEFAULT 1,
  sync_status           TEXT NOT NULL DEFAULT 'synced',
  last_calendar_updated TIMESTAMPTZ,
  last_seen_at          TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (calendar_id, google_event_id)
);

CREATE TABLE sync_outbox (
  id                 BIGSERIAL PRIMARY KEY,
  appointment_id     TEXT NOT NULL,
  calendar_id        TEXT NOT NULL REFERENCES sync_calendars(id),
  operation          TEXT NOT NULL,
  idempotency_key    TEXT NOT NULL UNIQUE,
  payload            JSONB NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending',
  attempts           INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  google_event_id    TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- This workflow only DRAINS this table -- something else (e.g. the
-- confirmation/cancellation workflow) must INSERT rows into it. See
-- Outbox and reconciliation design above.

CREATE TABLE sync_conflicts (
  id             BIGSERIAL PRIMARY KEY,
  appointment_id TEXT,
  calendar_id    TEXT NOT NULL,
  google_event_id TEXT,
  reason         TEXT NOT NULL,
  details        JSONB,
  status         TEXT NOT NULL DEFAULT 'open',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
CREATE OR REPLACE FUNCTION sync_lease_acquire(p_calendar_id text, p_owner text, p_ttl_seconds integer)
 RETURNS TABLE(out_acquired boolean, out_google_calendar_id text, out_sync_token text, out_pending_page_token text, out_needs_full_resync boolean, out_full_sync_started_at timestamp with time zone)
 LANGUAGE plpgsql
AS $$
DECLARE
  v_row sync_calendars%ROWTYPE;
BEGIN
  UPDATE sync_calendars sc
  SET lease_owner = p_owner,
      lease_expires_at = now() + (p_ttl_seconds || ' seconds')::interval,
      full_sync_started_at = CASE
        WHEN sc.needs_full_resync AND sc.pending_page_token IS NULL THEN now()
        ELSE sc.full_sync_started_at
      END
  WHERE sc.id = p_calendar_id
    AND (sc.lease_owner IS NULL OR sc.lease_expires_at < now())
  RETURNING sc.* INTO v_row;

  IF FOUND THEN
    RETURN QUERY SELECT true, v_row.google_calendar_id, v_row.sync_token,
      v_row.pending_page_token, v_row.needs_full_resync, v_row.full_sync_started_at;
    RETURN;
  END IF;

  RETURN QUERY SELECT false, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::BOOLEAN, NULL::TIMESTAMPTZ;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_lease_release(p_calendar_id text, p_owner text)
 RETURNS TABLE(out_released boolean)
 LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE sync_calendars sc
  SET lease_owner = NULL, lease_expires_at = NULL
  WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner;
  RETURN QUERY SELECT FOUND;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_lease_renew(p_calendar_id text, p_owner text, p_ttl_seconds integer)
 RETURNS TABLE(out_renewed boolean)
 LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE sync_calendars sc
  SET lease_expires_at = now() + (p_ttl_seconds || ' seconds')::interval
  WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now();
  RETURN QUERY SELECT FOUND;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_outbox_claim_batch(p_calendar_id text, p_owner text, p_limit integer, p_staleness_seconds integer)
 RETURNS TABLE(out_id bigint, out_appointment_id text, out_operation text, out_idempotency_key text, out_payload jsonb, out_google_event_id text, out_current_etag text, out_attempts integer)
 LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM 1 FROM sync_calendars sc
    WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now();
  IF NOT FOUND THEN
    RETURN;
  END IF;

  RETURN QUERY
  UPDATE sync_outbox o
  SET status = 'in_flight', attempts = o.attempts + 1, updated_at = now()
  WHERE o.id IN (
    SELECT s.id FROM sync_outbox s
    WHERE s.calendar_id = p_calendar_id
      AND (s.status IN ('pending', 'failed')
           OR (s.status = 'in_flight' AND s.updated_at < now() - (p_staleness_seconds || ' seconds')::interval))
    ORDER BY s.id
    LIMIT p_limit
    FOR UPDATE SKIP LOCKED
  )
  RETURNING o.id, o.appointment_id, o.operation, o.idempotency_key, o.payload,
    COALESCE(o.google_event_id, (SELECT m.google_event_id FROM appointment_calendar_mappings m WHERE m.appointment_id = o.appointment_id AND m.calendar_id = o.calendar_id)),
    (SELECT m.etag FROM appointment_calendar_mappings m WHERE m.appointment_id = o.appointment_id AND m.calendar_id = o.calendar_id),
    o.attempts;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_outbox_finalize(p_outcome text, p_outbox_id bigint, p_calendar_id text, p_owner text, p_appointment_id text, p_operation text, p_google_event_id text, p_etag text, p_updated text, p_reason text, p_details jsonb)
 RETURNS TABLE(out_ok boolean, out_reason text)
 LANGUAGE plpgsql
AS $$
DECLARE
  v_ok BOOLEAN;
BEGIN
  IF p_outcome = 'success' THEN
    RETURN QUERY SELECT * FROM sync_outbox_finalize_success(
      p_outbox_id, p_calendar_id, p_owner, p_appointment_id, p_operation, p_google_event_id, p_etag, p_updated
    );
    RETURN;
  ELSIF p_outcome = 'conflict' THEN
    SELECT f.out_ok INTO v_ok FROM sync_outbox_finalize_conflict(
      p_outbox_id, p_calendar_id, p_owner, p_appointment_id, p_google_event_id, p_reason, p_details
    ) f;
  ELSE
    SELECT f.out_ok INTO v_ok FROM sync_outbox_finalize_failure(
      p_outbox_id, p_calendar_id, p_owner, p_reason
    ) f;
  END IF;

  RETURN QUERY SELECT v_ok, p_reason;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_outbox_finalize_conflict(p_outbox_id bigint, p_calendar_id text, p_owner text, p_appointment_id text, p_google_event_id text, p_reason text, p_details jsonb)
 RETURNS TABLE(out_ok boolean)
 LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM 1 FROM sync_calendars sc
    WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now();
  IF NOT FOUND THEN
    RETURN QUERY SELECT false;
    RETURN;
  END IF;

  UPDATE sync_outbox o SET status = 'conflict', last_error = p_reason, updated_at = now() WHERE o.id = p_outbox_id;
  UPDATE appointment_calendar_mappings m SET sync_status = 'conflict', updated_at = now()
    WHERE m.appointment_id = p_appointment_id AND m.calendar_id = p_calendar_id;
  INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
  VALUES (p_appointment_id, p_calendar_id, p_google_event_id, p_reason, p_details);

  RETURN QUERY SELECT true;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_outbox_finalize_failure(p_outbox_id bigint, p_calendar_id text, p_owner text, p_error text)
 RETURNS TABLE(out_ok boolean)
 LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM 1 FROM sync_calendars sc
    WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now();
  IF NOT FOUND THEN
    RETURN QUERY SELECT false;
    RETURN;
  END IF;

  UPDATE sync_outbox o SET status = 'failed', last_error = p_error, updated_at = now() WHERE o.id = p_outbox_id;
  RETURN QUERY SELECT true;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_outbox_finalize_success(p_outbox_id bigint, p_calendar_id text, p_owner text, p_appointment_id text, p_operation text, p_google_event_id text, p_etag text, p_updated text)
 RETURNS TABLE(out_ok boolean, out_reason text)
 LANGUAGE plpgsql
AS $$
DECLARE
  v_updated TIMESTAMPTZ;
BEGIN
  PERFORM 1 FROM sync_calendars sc
    WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now();
  IF NOT FOUND THEN
    RETURN QUERY SELECT false, 'lease_not_owned'::TEXT;
    RETURN;
  END IF;

  -- Defense in depth: never let a malformed provider timestamp abort
  -- this call. n8n already bound-validates p_updated as a short string
  -- before calling this function, but format validity (a genuine
  -- parseable timestamp) is verified here, exception-safe.
  v_updated := NULL;
  IF p_updated IS NOT NULL THEN
    BEGIN
      v_updated := p_updated::TIMESTAMPTZ;
    EXCEPTION WHEN OTHERS THEN
      v_updated := NULL;
    END;
  END IF;

  UPDATE sync_outbox o
  SET status = 'applied', google_event_id = p_google_event_id, updated_at = now()
  WHERE o.id = p_outbox_id;

  IF p_operation = 'cancel' THEN
    UPDATE appointment_calendar_mappings m
    SET sync_status = 'calendar_cancelled', etag = p_etag, last_calendar_updated = v_updated,
        last_seen_at = now(), updated_at = now()
    WHERE m.appointment_id = p_appointment_id AND m.calendar_id = p_calendar_id;
  ELSE
    INSERT INTO appointment_calendar_mappings
      (appointment_id, calendar_id, google_event_id, etag, mapping_version, sync_status, last_calendar_updated, last_seen_at)
    VALUES (p_appointment_id, p_calendar_id, p_google_event_id, p_etag, 1, 'synced', v_updated, now())
    ON CONFLICT (appointment_id) DO UPDATE
    SET google_event_id = EXCLUDED.google_event_id, etag = EXCLUDED.etag,
        mapping_version = appointment_calendar_mappings.mapping_version + 1,
        sync_status = 'synced', last_calendar_updated = EXCLUDED.last_calendar_updated,
        last_seen_at = now(), updated_at = now();
  END IF;

  RETURN QUERY SELECT true, 'ok'::TEXT;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_process_page(p_calendar_id text, p_owner text, p_items jsonb, p_next_page_token text, p_next_sync_token text, p_is_final_page boolean, p_is_full_sync boolean)
 RETURNS TABLE(out_ok boolean, out_reason text, out_applied_count integer, out_tombstoned_count integer, out_unknown_count integer, out_malformed_count integer, out_conflict_count integer)
 LANGUAGE plpgsql
AS $$
DECLARE
  v_item JSONB;
  v_event_id TEXT;
  v_status TEXT;
  v_etag TEXT;
  v_updated_raw TEXT;
  v_updated TIMESTAMPTZ;
  v_appt_id TEXT;
  v_appt_id_raw TEXT;
  v_appt_id_valid BOOLEAN;
  v_valid BOOLEAN;
  v_projected JSONB;
  v_existing appointment_calendar_mappings%ROWTYPE;
  v_appt_row appointments%ROWTYPE;
  v_other_mapping appointment_calendar_mappings%ROWTYPE;
  v_applied INT := 0;
  v_tombstoned INT := 0;
  v_unknown INT := 0;
  v_malformed INT := 0;
  v_conflict INT := 0;
  v_full_sync_started_at TIMESTAMPTZ;
BEGIN
  PERFORM 1 FROM sync_calendars sc
    WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now()
    FOR UPDATE;
  IF NOT FOUND THEN
    RETURN QUERY SELECT false, 'lease_not_owned'::TEXT, 0, 0, 0, 0, 0;
    RETURN;
  END IF;

  -- ---------------------------------------------------------------
  -- Page-level validation: fail closed and leave the cursor exactly
  -- where it was on anything that doesn't look like a genuine Google
  -- Calendar events.list page. This runs BEFORE any per-item work or
  -- any cursor-advancing write, so a malformed page can never advance
  -- past unprocessed data.
  -- ---------------------------------------------------------------
  IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' THEN
    RETURN QUERY SELECT false, 'invalid_items_shape'::TEXT, 0, 0, 0, 0, 0;
    RETURN;
  END IF;

  IF jsonb_array_length(p_items) > 2500 THEN
    RETURN QUERY SELECT false, 'page_too_large'::TEXT, 0, 0, 0, 0, 0;
    RETURN;
  END IF;

  IF p_next_page_token IS NOT NULL AND length(p_next_page_token) > 2048 THEN
    RETURN QUERY SELECT false, 'invalid_page_token'::TEXT, 0, 0, 0, 0, 0;
    RETURN;
  END IF;

  IF p_next_sync_token IS NOT NULL AND length(p_next_sync_token) > 2048 THEN
    RETURN QUERY SELECT false, 'invalid_sync_token'::TEXT, 0, 0, 0, 0, 0;
    RETURN;
  END IF;

  FOR v_item IN SELECT jsonb_array_elements(p_items) LOOP
    BEGIN
      -- Every per-item code path below is inside this nested block so
      -- that ANY unexpected error for one malformed item (a bad cast,
      -- an unexpected type) is caught here and counted as malformed --
      -- it can never abort the rest of the page or the cursor advance.

      IF jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
        v_malformed := v_malformed + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (NULL, p_calendar_id, NULL, 'malformed_event', jsonb_build_object('reason', 'not_an_object'));
        CONTINUE;
      END IF;

      v_event_id := v_item->>'id';
      v_status := v_item->>'status';
      v_etag := v_item->>'etag';
      v_updated_raw := v_item->>'updated';
      v_appt_id_raw := v_item#>>'{extendedProperties,private,appointmentId}';

      -- Bounded, allowlist validation of every provider-controlled
      -- field before it is cast, stored, or used to build a URL.
      v_valid := v_event_id IS NOT NULL
        AND length(v_event_id) BETWEEN 1 AND 1024
        AND v_event_id ~ '^[A-Za-z0-9_-]+$'
        AND v_status IN ('confirmed', 'cancelled', 'tentative')
        AND (v_etag IS NULL OR length(v_etag) <= 512)
        AND v_updated_raw IS NOT NULL
        AND length(v_updated_raw) <= 64;

      v_updated := NULL;
      IF v_valid THEN
        BEGIN
          v_updated := v_updated_raw::TIMESTAMPTZ;
        EXCEPTION WHEN OTHERS THEN
          v_valid := false;
        END;
      END IF;

      IF NOT v_valid THEN
        v_malformed := v_malformed + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (
          NULL, p_calendar_id,
          CASE WHEN v_event_id IS NOT NULL AND length(v_event_id) <= 1024 THEN left(v_event_id, 1024) ELSE NULL END,
          'malformed_event', jsonb_build_object('reason', 'invalid_or_unbounded_field')
        );
        CONTINUE;
      END IF;

      v_appt_id_valid := v_appt_id_raw IS NOT NULL
        AND length(v_appt_id_raw) BETWEEN 1 AND 128
        AND v_appt_id_raw ~ '^[A-Za-z0-9_-]+$';
      v_appt_id := CASE WHEN v_appt_id_valid THEN v_appt_id_raw ELSE NULL END;

      -- Sanitized, bounded projection -- the ONLY shape ever persisted
      -- for this event. Never the raw provider object: no summary,
      -- description, location, attendees, organizer, conferenceData,
      -- or attachments are ever read from v_item again below.
      v_projected := jsonb_build_object(
        'event_id', left(v_event_id, 1024),
        'status', v_status,
        'etag', CASE WHEN v_etag IS NOT NULL THEN left(v_etag, 512) ELSE NULL END,
        'updated', v_updated,
        'appointment_id_marker', v_appt_id
      );

      IF v_appt_id_raw IS NOT NULL AND NOT v_appt_id_valid THEN
        -- A marker is present but fails the bounded allowlist --
        -- corrupt/untrustworthy data, never used for matching.
        v_malformed := v_malformed + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (NULL, p_calendar_id, left(v_event_id, 1024), 'malformed_event', v_projected);
        CONTINUE;
      END IF;

      IF v_appt_id IS NULL THEN
        v_unknown := v_unknown + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (NULL, p_calendar_id, left(v_event_id, 1024), 'unknown_event', v_projected);
        CONTINUE;
      END IF;

      SELECT * INTO v_existing FROM appointment_calendar_mappings m
        WHERE m.calendar_id = p_calendar_id AND m.google_event_id = v_event_id;

      IF NOT FOUND THEN
        SELECT * INTO v_appt_row FROM appointments a WHERE a.appointment_id = v_appt_id;
        IF NOT FOUND THEN
          v_unknown := v_unknown + 1;
          INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
          VALUES (v_appt_id, p_calendar_id, left(v_event_id, 1024), 'unknown_appointment_reference', v_projected);
          CONTINUE;
        END IF;

        SELECT * INTO v_other_mapping FROM appointment_calendar_mappings m2 WHERE m2.appointment_id = v_appt_id;
        IF FOUND THEN
          v_conflict := v_conflict + 1;
          INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
          VALUES (v_appt_id, p_calendar_id, left(v_event_id, 1024), 'duplicate_mapping_candidate', v_projected);
          CONTINUE;
        END IF;

        IF v_status = 'cancelled' THEN
          v_tombstoned := v_tombstoned + 1;
          INSERT INTO appointment_calendar_mappings
            (appointment_id, calendar_id, google_event_id, etag, mapping_version, sync_status, last_calendar_updated, last_seen_at)
          VALUES (v_appt_id, p_calendar_id, v_event_id, v_etag, 1, 'calendar_cancelled', v_updated, now());
          CONTINUE;
        END IF;

        v_applied := v_applied + 1;
        INSERT INTO appointment_calendar_mappings
          (appointment_id, calendar_id, google_event_id, etag, mapping_version, sync_status, last_calendar_updated, last_seen_at)
        VALUES (v_appt_id, p_calendar_id, v_event_id, v_etag, 1, 'synced', v_updated, now());
        CONTINUE;
      END IF;

      IF v_existing.appointment_id <> v_appt_id THEN
        v_conflict := v_conflict + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (v_existing.appointment_id, p_calendar_id, left(v_event_id, 1024), 'appointment_id_mismatch', v_projected);
        UPDATE appointment_calendar_mappings m2 SET last_seen_at = now()
          WHERE m2.appointment_id = v_existing.appointment_id AND m2.calendar_id = p_calendar_id;
        CONTINUE;
      END IF;

      IF v_status = 'cancelled' THEN
        v_tombstoned := v_tombstoned + 1;
        IF v_existing.sync_status <> 'calendar_cancelled' THEN
          INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
          VALUES (v_appt_id, p_calendar_id, left(v_event_id, 1024), 'calendar_side_cancellation', v_projected);
        END IF;
        UPDATE appointment_calendar_mappings m3
        SET sync_status = 'calendar_cancelled', etag = v_etag, last_calendar_updated = v_updated,
            last_seen_at = now(), updated_at = now()
        WHERE m3.appointment_id = v_appt_id AND m3.calendar_id = p_calendar_id;
        CONTINUE;
      END IF;

      IF v_existing.etag IS NOT NULL AND v_existing.etag <> v_etag AND v_existing.sync_status = 'synced' THEN
        v_conflict := v_conflict + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (v_appt_id, p_calendar_id, left(v_event_id, 1024), 'concurrent_edit', v_projected);
        UPDATE appointment_calendar_mappings m4
        SET sync_status = 'conflict', etag = v_etag, last_calendar_updated = v_updated,
            last_seen_at = now(), updated_at = now()
        WHERE m4.appointment_id = v_appt_id AND m4.calendar_id = p_calendar_id;
        CONTINUE;
      END IF;

      v_applied := v_applied + 1;
      UPDATE appointment_calendar_mappings m5
      SET etag = v_etag, last_calendar_updated = v_updated,
          last_seen_at = now(), updated_at = now()
      WHERE m5.appointment_id = v_appt_id AND m5.calendar_id = p_calendar_id;

    EXCEPTION WHEN OTHERS THEN
      -- Defense in depth: any error this far down for one item (a
      -- constraint violation, an unexpected type coercion failure)
      -- must not abort the page. Counted as malformed; nothing about
      -- this specific item is persisted.
      v_malformed := v_malformed + 1;
    END;
  END LOOP;

  SELECT sc2.full_sync_started_at INTO v_full_sync_started_at FROM sync_calendars sc2 WHERE sc2.id = p_calendar_id;

  UPDATE sync_calendars sc3
  SET pending_page_token = p_next_page_token,
      sync_token = CASE WHEN p_is_final_page THEN p_next_sync_token ELSE sc3.sync_token END,
      needs_full_resync = CASE WHEN p_is_final_page THEN false ELSE sc3.needs_full_resync END,
      last_synced_at = now(),
      last_full_sync_at = CASE WHEN p_is_final_page AND p_is_full_sync THEN now() ELSE sc3.last_full_sync_at END,
      full_sync_started_at = CASE WHEN p_is_final_page THEN NULL ELSE sc3.full_sync_started_at END
  WHERE sc3.id = p_calendar_id;

  IF p_is_final_page AND p_is_full_sync AND v_full_sync_started_at IS NOT NULL THEN
    INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
    SELECT m6.appointment_id, m6.calendar_id, m6.google_event_id, 'missing_from_calendar',
           jsonb_build_object('last_seen_at', m6.last_seen_at)
    FROM appointment_calendar_mappings m6
    WHERE m6.calendar_id = p_calendar_id
      AND m6.sync_status NOT IN ('calendar_cancelled')
      AND (m6.last_seen_at IS NULL OR m6.last_seen_at < v_full_sync_started_at)
      AND NOT EXISTS (
        SELECT 1 FROM sync_conflicts sc4
        WHERE sc4.appointment_id = m6.appointment_id AND sc4.reason = 'missing_from_calendar' AND sc4.status = 'open'
      );
  END IF;

  RETURN QUERY SELECT true, 'ok'::TEXT, v_applied, v_tombstoned, v_unknown, v_malformed, v_conflict;
END;
$$
;

CREATE OR REPLACE FUNCTION sync_reset_for_full_resync(p_calendar_id text, p_owner text)
 RETURNS TABLE(out_ok boolean)
 LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE sync_calendars sc
  SET sync_token = NULL, pending_page_token = NULL, needs_full_resync = true, full_sync_started_at = NULL
  WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now();
  RETURN QUERY SELECT FOUND;
END;
$$
;
```

The ten functions were rewritten in this round to fix the two bugs described in [Feasibility investigation](#feasibility-investigation) (`sync_outbox_finalize`'s ambiguous `out_ok` reference) and to add the bounded, allowlist projection and per-item exception safety described in [Data-minimization design](#data-minimization-design) (`sync_process_page`). Every function follows the pattern established by this repository's other Postgres-backed workflows: `RETURNS TABLE` columns are prefixed (`out_...`) to avoid ambiguity with real table column names inside the function body, and every function always returns exactly one row (or, for the batch-claim function, zero-to-N rows by design, handled on the n8n side via `alwaysOutputData` — see [Feasibility investigation](#feasibility-investigation)).

## Migration notes for an existing installation

If you already installed the schema and functions from an earlier round of this workflow, re-run the full function block above (`CREATE OR REPLACE FUNCTION` is safe to re-apply) — no table structure changed, only function bodies. Specifically:

- `sync_process_page` gained page-level and per-item validation, bounded projection, and exception-safe per-item handling. Any `sync_conflicts` rows written by the *old* version of this function may contain full raw event payloads rather than the new five-field allowlist — if this matters for your compliance posture, purge or redact pre-existing `sync_conflicts.details` values written before this update.
- `sync_outbox_finalize` had an ambiguous `out_ok` column reference that made every `conflict`/`failure` outbox finalize call throw a hard Postgres error, aborting the execution rather than resolving the outbox row. If you deployed the earlier version, any execution that ever hit an outbox conflict or failure would have failed the *entire* workflow run rather than continuing — check for unexpectedly-`in_flight` outbox rows and re-run this workflow after upgrading the function.
- `sync_outbox_finalize_success` now validates the `updated` timestamp in an exception-safe block before casting, instead of letting a malformed value abort the call.

## Setup steps

1. Create the schema — see [Schema](#schema). If you already have `appointments` from `whatsapp-appointment-confirmation-cancellation`, skip that table.
2. Import `google-calendar-postgres-sync.json`.
3. Create and bind your Postgres credential (see [Required credentials](#required-credentials)) to the eight Postgres nodes listed there.
4. Create and bind your Google Calendar OAuth2 API credential to the four HTTP Request nodes listed there.
5. Insert one row into `sync_calendars` per calendar you want synced, with its real `google_calendar_id` (e.g. `primary`, or a shared calendar's address) — this is what `syncCalendarId` will reference. Leave `sync_token`/`pending_page_token` `NULL` and `needs_full_resync` at its default `true`; the first run will do a full sync.
6. Build whatever enqueues `sync_outbox` rows for appointment changes you want pushed to Calendar (this workflow does not do that itself — see [Outbox and reconciliation design](#outbox-and-reconciliation-design)), and whatever triggers this workflow periodically (a Schedule Trigger calling it via Execute Workflow, once per calendar you're syncing).
7. Have a plan for resolving `sync_conflicts` rows and `conflict`-status outbox/mapping rows — see the table in [Outbox and reconciliation design](#outbox-and-reconciliation-design). This workflow deliberately never auto-resolves any of them.
8. Test with synthetic data against your own isolated setup first — see [Test procedure](#test-procedure) for how this package itself was tested (against a mock Calendar server, never live Google).

## Known limitations

- **Does not enqueue outbox rows itself.** Something else must `INSERT` into `sync_outbox` when an appointment changes.
- **Does not resolve any conflict automatically**, by design — every conflict reason in [Synchronization authority and conflict policy](#synchronization-authority-and-conflict-policy) requires a human or a separate, out-of-scope reconciliation process.
- **One calendar per execution.** Syncing N calendars means N scheduled invocations (or N sequential calls from an orchestrator), each with its own `syncCalendarId`.
- **No automatic retry of a `conflict` or `failed` outbox row beyond what staleness-based reclaim already provides** — a `failed` row is reclaimed by the *next* drain run, not retried in-line.
- **The real Google Calendar API was never contacted during testing** — verified only through a temporary, uncommitted mock-bound copy, disclosed precisely in [Test procedure](#test-procedure). Verify against your own real, non-production calendar before relying on this.
- **Recurring events**: `singleEvents=true` is used consistently for both full and incremental sync, so recurring events are synced as individual instances, not as a master event with expansion logic — this keeps matching behavior identical between full and incremental sync, but means a recurrence *pattern* change on Calendar's side surfaces as many individual instance conflicts, not one.
- **A pull-sync that runs immediately after an outbox-created event, within the same or a very soon following execution, will legitimately re-see that event** (its sync token was issued before the creation) and re-apply its own already-correct etag — this is honest, correct incremental-sync behavior, not a bug, and was specifically what surfaced the mock server's own incremental-sync fidelity gap during this round's testing (see [Feasibility investigation](#feasibility-investigation)).
- This workflow has been verified as a template against the specific n8n, Node.js, and PostgreSQL versions documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads `syncCalendarId` from its caller — never a Calendar id or event id, both of which are read exclusively from Postgres. Reads and writes `appointment_calendar_mappings`, `sync_calendars` (cursor/lease state), `sync_outbox` (drains, never enqueues), and `sync_conflicts`. Every value ever persisted from a Calendar-provided event is limited to a bounded five-field allowlist (event id, status, etag, updated timestamp, appointment id marker) — see [Data-minimization design](#data-minimization-design); no summary, description, location, attendee data, organizer data, conference links, or attachments are ever read past the point where they're projected out. Makes outbound HTTP calls only to the fixed Google Calendar API host, only for calendars and events already validated against Postgres state. Its final summary output contains only aggregate counts (`pull.pages/applied/tombstoned/unknown/malformed/conflict`, `outbox.claimed/applied/conflicted/failed`) and status — never customer message text, calendar event bodies, credentials, or raw Postgres rows.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-20
