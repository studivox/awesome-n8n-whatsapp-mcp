# Google Calendar ↔ Postgres Synchronization

## What it does

A reusable **sub-workflow** (Execute Workflow Trigger — meant to be called on a schedule or by an orchestrator, not exposed as a webhook) that keeps one Google Calendar in sync with Postgres, in both directions, without ever letting either side silently overwrite the other:

- **Pull-sync** (Calendar → Postgres): fetches new/changed/cancelled events via `events.list`, paginated, using an incremental sync token when available and falling back to a full resync when the token expires (`410 Gone`) or none exists yet. This direction never mutates an appointment's real content — it only refreshes cached drift-detection metadata (`etag`, `updated`, tombstone state) and raises a controlled conflict when something changed on the Calendar side that Postgres didn't originate.
- **Outbox drain** (Postgres → Calendar): applies pending `create`/`update`/`cancel` operations queued in `sync_outbox` by whatever mutates an appointment (this workflow does not enqueue outbox rows itself — see [Outbox and reconciliation design](#outbox-and-reconciliation-design)). Creates are idempotency-checked before ever calling `POST`; updates are conditional (`If-Match`) so a Calendar-side edit this workflow doesn't know about is rejected as a conflict, never blindly overwritten.

It accepts exactly one input: `syncCalendarId`, an **internal id** referencing a row in the `sync_calendars` table. **The real Google Calendar id is never a caller input** — it's read exclusively from that row, so arbitrary caller-supplied data can never redirect a Calendar API call to a calendar this deployment doesn't own. The Calendar API host is fixed (`https://www.googleapis.com/calendar/v3`).

A single row-based **lease** (not `pg_advisory_lock` — see [Cursor, locking, and pagination design](#cursor-locking-and-pagination-design) for why) ensures only one runner touches a given calendar's pull-sync *and* outbox drain at a time, and self-heals on a fixed TTL if a runner crashes without releasing it.

## Real business use case

Appointments get created, confirmed, cancelled, and rescheduled in Postgres (for example, by [`whatsapp-appointment-confirmation-cancellation`](whatsapp-appointment-confirmation-cancellation.md)). Someone on staff also edits the calendar directly sometimes — moves a meeting, cancels one from their phone. Without a reconciliation loop, these two views of "what's actually happening" drift apart silently. This workflow is that reconciliation loop: it pushes Postgres's outbox of pending Calendar operations out, and it pulls Calendar's own state back in far enough to notice — and flag, never silently resolve — anywhere the two disagree.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2, against an isolated **PostgreSQL 16.15** instance for testing.

## Feasibility investigation

| Question | Finding |
|---|---|
| Can a Postgres-node-based lease survive n8n's connection pooling? | **Central finding.** n8n's Postgres node checks a client out of a pool per query and returns it after — it does not guarantee the same physical session across separate node calls in one execution. Empirically, sequential Postgres-node calls within one execution consistently reused the same backend pid, even under 5-way concurrent load (verified via `pg_backend_pid()` probes) — but this is undocumented pooling behavior, not a guaranteed contract, and even if it held, a session-scoped `pg_advisory_lock` only releases when *that* session disconnects, which a crashed runner would leave leaked until the pool eventually recycles the connection. A row-based lease (ordinary compare-and-set `UPDATE`s) has no connection-affinity dependency and self-heals on a fixed TTL regardless of how a runner died. Used the lease design. |
| Does n8n support the open-ended "keep fetching while there's a next page token" loop this design needs? | Yes — verified directly: a cyclic back-edge connection (an IF node's "true" output wired back to an earlier node) executes correctly across multiple passes, with node state correctly correlating to the *current* iteration (not always the first) when referenced by name from a later node in the same loop. Confirmed with an explicit counter test (loop ran exactly 3 times, accumulated `[1,2,3]`) before relying on it for pagination. |
| Does a zero-row Postgres result matter for n8n's execution model here too? | Yes, same finding as prior workflows in this repository — zero rows means zero items means every downstream node is silently skipped. The outbox claim step can legitimately return 0..N rows (a real per-item fan-out, not a fixed single-row status check), so `alwaysOutputData: true` is set on that node specifically — verified experimentally that this makes it emit exactly one empty placeholder item on a zero-row result, letting the very next node distinguish "nothing claimed" from "N items claimed" instead of silently not running at all. |
| Does one HTTP Request node support per-item, expression-driven method/URL/body? | Yes — confirmed from n8n's own HTTP Request node source: the method parameter is read via `getNodeParameter('method', itemIndex)`, the same per-item resolution as every other parameter. This let one node handle both `update` (`PATCH`) and `cancel` (`DELETE`) per claimed outbox row via an expression, instead of two separate branches. |
| Does a Code node in "Run Once for Each Item" mode behave the way this design's per-item outbox classification needs? | Partially as expected, with two corrections found only by testing: (1) `$input.first()/.last()/.all()/.itemMatching()` are explicitly disallowed in that mode (n8n throws before running the code) — `$json` is the correct per-item accessor. (2) The mode requires returning a bare object (`return { json: {...} }`), not an array — returning an array throws `A 'json' property isn't an object`. Both were caught by execution errors during testing and fixed; see [Test procedure](#test-procedure). |
| Does `$('NodeName').first()`, referenced from inside a per-item Code node, correctly correlate to the *current* item, or always item 0? | **Always the first item of whatever that node produced — not item-correlated.** This does not trip n8n's each-item-mode validator (only bare `$input.*` is blocked), so it fails silently rather than throwing. Confirmed via a genuine 2-item batch (one `update`, one `cancel` outbox row, targeting different appointments): with `.first()`, both items' finalize calls would have used the *first* item's identifiers. Fixed by using `$('NodeName').item.json` (paired-item aware) everywhere a per-item node needs to reach an upstream per-item node by name. Re-verified with the same 2-item batch: each item ended up mapped to its own, distinct Calendar event, not cross-contaminated. |
| Does a Postgres "Execute Query" node's own output silently discard the item's other fields? | Yes, confirmed repeatedly — its output is exactly the query's result columns, nothing carried over from the incoming item. Every node downstream of a Postgres call in this workflow that also needs earlier context (e.g. the finalize step's `outcome` field, needed for the final summary) reaches back to the pre-Postgres node by name rather than assuming the Postgres node's own output still has it. This is the same category of finding documented in this repository's other Postgres-backed workflows, now also confirmed for a genuinely branching, multi-item graph. |
| Can the real Google Calendar API be used for testing? | No — this project has no dedicated synthetic test calendar or credential available. Per this repository's testing policy, **the real Google Calendar API was never contacted.** All Calendar-dependent testing used a temporary, uncommitted mock-bound copy of this workflow (identical to the committed file except the Calendar host points at a local mock HTTP server and the credential is a synthetic one), disclosed here and in every relevant results section below. |

**Feasibility verdict: GO**, with the design corrected per the findings above before any test was counted as passing.

## Synchronization authority and conflict policy

- **Postgres is authoritative for appointment identity and for the appointment ↔ Calendar-event mapping.** The mapping (`appointment_calendar_mappings`) is only ever created two ways: (a) the outbox drain creates a Calendar event and records the mapping it just made, or (b) a pull-sync full resync adopts a *pre-existing* Calendar event for a known appointment — but **only** when that event carries a marker (`extendedProperties.private.appointmentId`) matching a real row already in `appointments`, and only when that appointment doesn't already have a different mapping. An event that isn't marked, or is marked with an appointment id Postgres doesn't recognize, is never adopted — it's recorded as a conflict for a human to resolve.
- **Never matched by name, title, phone number, or email.** The only identifier ever used to correlate a Calendar event with an appointment is the stable, client-generated `appointmentId` written into `extendedProperties.private` at creation time.
- **Never a caller-controlled Calendar event id or Calendar id.** `syncCalendarId` (the only caller input) is validated as an opaque internal identifier and used solely as a `WHERE` parameter against `sync_calendars`; the real `google_calendar_id` it resolves to, and every Calendar event id this workflow acts on, come exclusively from that row or from prior Postgres state (`appointment_calendar_mappings.google_event_id`) — never from the trigger's input or from unvalidated Calendar response data used as-is.
- **Conflicts are never silently resolved in either direction.** Four distinct conflict reasons are recorded in `sync_conflicts`, each requiring a human or a separate reconciliation process to resolve — see the table in [Outbox and reconciliation design](#outbox-and-reconciliation-design):
  - `unknown_appointment_reference` — a Calendar event's marker references an appointment id Postgres has never heard of.
  - `duplicate_mapping_candidate` — a Calendar event's marker references an appointment that's already mapped to a *different* event.
  - `concurrent_edit` — a mapped event's `etag` changed on the Calendar side without a corresponding outbox push explaining it.
  - `calendar_side_cancellation` — a mapped event was cancelled directly on Calendar, not through the outbox.
  - `missing_from_calendar` — a full resync completed without ever seeing a mapped, non-cancelled event again (it vanished from Calendar's own listing).
  - `appointment_id_mismatch` — an incoming event's marker no longer matches the appointment id this workflow's own mapping recorded for that exact Calendar event id.

## Cursor, locking, and pagination design

**Lease, not `pg_advisory_lock`.** See the feasibility table above for why. `sync_calendars` carries `lease_owner`/`lease_expires_at`; acquiring is a single compare-and-set `UPDATE … WHERE lease_owner IS NULL OR lease_expires_at < now()`. A second runner attempting the same calendar while the lease is held gets `lease_busy` immediately — verified to make **zero** Calendar or further Postgres calls in that case.

**Durable per-calendar cursor**, also on `sync_calendars`: `sync_token` (Google's incremental token; `NULL` means a full resync is owed), `pending_page_token` (mid-pagination resume point), `needs_full_resync`, `full_sync_started_at` (stamped when a full-resync run begins, used to detect events that vanished from Calendar entirely — see `missing_from_calendar` above).

**Pagination**: each page is applied and the cursor advanced **in the same Postgres statement** (`sync_process_page`) — the durable state after that call is *either* "this page fully applied and the cursor points past it" *or* (on any failure inside that call) "nothing about this page happened and the cursor is exactly where it was." There is no reachable state in between. Verified directly via crash-injection (see [Crash-consistency results](#crash-consistency-results)).

**Expired sync token (`410 Gone`)**: caught, the cursor is reset to a clean full-resync state (`sync_reset_for_full_resync`, itself lease-guarded and idempotent), and the pagination loop restarts as a full sync from the beginning — verified end-to-end through the real workflow: a token deliberately invalidated mid-test still produced a complete, correct resync including a newly-created event the stale token predated.

**"Advance the cursor only after the complete applicable batch is processed successfully"**: satisfied at the page granularity described above (the unit n8n and the Calendar API bound work to), and re-verified specifically for a *partial-batch* failure via crash injection — see test #19 below.

## Outbox and reconciliation design

`sync_outbox` holds `pending` Postgres → Calendar operations; **this workflow only drains it, it does not enqueue rows** — that's the responsibility of whatever mutates an appointment (e.g. the confirmation/cancellation workflow), matching this repository's existing pattern of each package staying scoped to one job. A drained row moves through `pending → in_flight → applied | conflict | failed`. A row stuck `in_flight` past a staleness threshold (a prior runner crashed after claiming it but before finalizing) is safely reclaimable by a later drain — safely *because*:

- **`create`** is idempotency-checked first: before ever calling `POST`, the workflow looks up whether an event carrying this outbox row's `idempotencyKey` already exists (`privateExtendedProperty` exact-match query). If a prior attempt's `POST` actually succeeded but the finalize write never completed (the crash gap), the retry adopts that existing event instead of creating a duplicate. Verified directly: after simulating exactly this crash, a recovery drain made **zero** additional `POST` calls and correctly adopted the already-created event.
- **`update`** is sent with an `If-Match: <etag>` header set to Postgres's currently-known etag for that mapping. A concurrent Calendar-side edit this workflow doesn't know about makes the etag stale, and Google's conditional-request semantics reject it with `412` — recorded as an explicit `conflict` outcome, never silently overwritten.
- **`cancel`** (`DELETE`, which Google marks the event cancelled rather than purging it) is naturally idempotent — repeating it against an already-cancelled event is a no-op.

Every finalize call (`sync_outbox_finalize`) is lease-guarded the same way the pull-sync writes are, and is a single statement regardless of outcome (success/conflict/failure), so there's one Postgres write path to reason about rather than three.

| Durable state | Meaning | Resolution |
|---|---|---|
| `sync_outbox.status = 'pending'`/`'failed'` | Not yet applied, or a prior attempt failed (network, non-2xx, etc.) | Reclaimed automatically by the next drain |
| `sync_outbox.status = 'in_flight'` past staleness | A runner claimed it and then crashed before finalizing | Reclaimed automatically by a later drain (idempotency-checked for `create`; naturally idempotent for `update`/`cancel`) |
| `sync_outbox.status = 'conflict'` / `appointment_calendar_mappings.sync_status = 'conflict'` | Calendar rejected the write (etag mismatch) or a pull-sync detected a Calendar-side edit this workflow didn't originate | Requires a human (or a separate, out-of-scope reconciliation workflow) to decide which side is correct |
| `sync_conflicts` row, any `reason` | See the six reasons listed under [Synchronization authority and conflict policy](#synchronization-authority-and-conflict-policy) | Same — a human decides; this workflow never auto-resolves any of them |

**This workflow never claims atomicity across Postgres and Google Calendar** — it's structurally impossible, and every design choice above (idempotency keys, conditional requests, lease-guarded finalize, staleness-based reclaim) exists specifically to make the *un-atomic* gap between "Calendar call happened" and "Postgres recorded it" safe to retry or safe to leave for reconciliation, never silently wrong.

## Crash-consistency results

All crash points below were tested via the same technique this repository has used previously: deliberately interrupting a step (never completing a Postgres write, or injecting a test-only `RAISE EXCEPTION` variant of a function, dropped after use) and inspecting Postgres directly afterward — not trusting the workflow's own returned status as proof.

| Crash point | Result |
|---|---|
| Between claiming an outbox row and the Calendar call | Row remains `pending`; never claimed as `in_flight` until an actual claim happens. |
| Calendar `create` call succeeds, then Postgres becomes unreachable before finalize | Outbox row left `in_flight`, `google_event_id` still `NULL` on that row (finalize never ran) — **not** falsely marked applied. A later drain's idempotency pre-check found the already-created event and adopted it, making zero additional `POST` calls. |
| A test-only variant of the page-processing function raises immediately after applying item 1 of a 2-item page, before the cursor-advance write | The entire call rolled back: **zero** rows in `appointment_calendar_mappings` for the crashed page, and the cursor (`sync_token`/`pending_page_token`) completely untouched — confirming a partial-batch failure never advances the cursor past unapplied work. |
| Postgres unreachable before a run starts | The lease-acquire call fails immediately and loudly; zero Calendar calls occur. |
| A calendar event carrying an unrecognized or mismatched marker | Never mutates `appointments` or an unrelated mapping — recorded as a conflict, mapping/appointment state left exactly as it was. |

## Test procedure

Built and verified in an isolated local n8n test environment (the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, isolated `N8N_USER_FOLDER` and SQLite database) against an isolated local **PostgreSQL 16.15** instance (fresh `initdb` cluster, non-default port, synthetic data only). **The real Google Calendar API was never contacted** — every test requiring a Calendar call used a temporary, uncommitted mock-bound copy of this workflow (same graph, Calendar host and credential swapped for a local mock HTTP server), never exported or committed.

Two layers of testing were used, both required for confidence in a design this concurrency-sensitive:

1. **Direct-SQL engine tests** — the exact orchestration logic (lease acquire → paginated pull-sync → outbox drain → lease release) implemented once more in a small Python harness calling the same PL/pgSQL functions and the same mock Calendar server, so the *engine's* correctness could be verified exhaustively and fast, independent of n8n's own execution-model quirks.
2. **Real n8n workflow tests** — the actual committed graph (or its temporary mock-bound copy), executed through n8n's own REST API, to verify the *wiring* — the two n8n-specific bugs in the table above (each-item-mode return shape, `.first()` not being paired-item-aware) were only found this way, not at the SQL layer.

| # | Scenario | Result | Verified via |
|---|---|---|---|
| 1 | Initial full synchronization | All pre-existing, appointment-marked events adopted; cursor set; `needs_full_resync` cleared | both layers |
| 2 | Incremental synchronization | Only the genuinely new event applied on the next run, using the stored sync token | both layers |
| 3 | Multiple pages | 7 events across 4 pages (page size 2) all correctly applied, pagination loop ran exactly 4 times | both layers |
| 4 | Empty / no-change run | Zero applied, zero errors | both layers |
| 5 | New mapped event (outbox create) | Event created, mapping recorded with real `google_event_id`/`etag` | both layers |
| 6 | Updated mapped event (outbox update) | Existing event's summary changed via `PATCH`; mapping's etag refreshed | both layers |
| 7 | Cancellation / deletion tombstone (outbox cancel) | Event marked `cancelled` on Calendar; mapping's `sync_status` set to `calendar_cancelled` | both layers |
| 8 | Unknown event without a trusted appointment mapping | Marker references an appointment id `appointments` has never heard of → `unknown_appointment_reference` conflict, zero mutation | both layers |
| 9 | Missing / malformed mapping metadata | Event with no `appointmentId` marker at all → `unknown_event` conflict, zero mutation | both layers |
| 10 | Duplicate delivery / replayed page | The exact same page applied twice → second application is a no-op (no duplicate mapping rows) | direct SQL |
| 11 | Out-of-order event data | A stale-etag page for an already-synced mapping → flagged `conflict`, not blindly accepted as newer truth | direct SQL |
| 12 | Concurrent sync runners, same calendar | 4 simultaneous runners: exactly 1 succeeds, 3 report `lease_busy`, zero duplicate mappings | direct SQL |
| 13 | Independent runners, different calendars | Two calendars synced fully concurrently, zero interference | direct SQL |
| 14 | Postgres/Calendar edit conflict | An event edited directly on Calendar (not via outbox) → next pull-sync detects the etag drift, flags `concurrent_edit`, does not overwrite anything in Postgres | both layers |
| 15 | Expired sync token, safe resync | Deliberately invalidated token → `410` caught, full resync restarted, all events (including one created after invalidation) correctly present afterward | both layers |
| 16 | Google 400 / 401 / 403 / 404 / 429 / 500 / timeout | Each handled without crashing the execution; reported as a controlled `calendar_error` with the real status; zero further mutation that run | direct SQL (mock server forced-status control endpoint) |
| 17 | PostgreSQL unavailable before processing | Lease-acquire fails immediately and loudly | direct SQL |
| 18 | PostgreSQL unavailable during finalization (crash after Calendar success) | Outbox row left `in_flight`, not falsely applied; a later recovery drain adopted the already-created event via its idempotency key with zero duplicate `POST`s | direct SQL |
| 19 | Cursor not advancing after a partial batch failure | Crash-injected mid-page: zero partial mapping rows, cursor completely untouched | direct SQL (crash-injection, test-only function variant, dropped after use) |
| 20 | Zero unauthorized Calendar calls on the lease-busy path | Confirmed via the mock server's own call log: zero calls while a lease was externally held | direct SQL + real n8n execution |
| — | Multi-page pull-sync through the real committed graph | 3 events across 2 pages, `pages: 2, applied: 3` in the final summary, loop ran exactly twice | real n8n execution |
| — | Outbox create with idempotency pre-check, through the real committed graph | `claimed: 1, applied: 1`; mapping recorded with the real Calendar-assigned event id | real n8n execution |
| — | Two distinct outbox items (one update, one cancel, different appointments) in the same batch, through the real committed graph | Both items correctly and *distinctly* applied — the specific bug found in feasibility testing (`.first()` not being item-correlated) is what this test was built to catch, and it did | real n8n execution |
| — | `410` recovery loop, through the real committed graph | `Fetch Events Page` ran 3 times (1 failed attempt + 2 full-resync pages), `Reset For Full Resync` ran once, all events present afterward | real n8n execution |
| — | Lease-busy path, through the real committed graph | Execution stopped at `Build Lease Busy Response` after 6 nodes; zero Postgres/Calendar work beyond the initial acquire attempt | real n8n execution |
| — | Non-410 Calendar error (500), through the real committed graph | `Build Calendar Error Response` → lease released cleanly, zero stuck lease afterward | real n8n execution |

All 27 direct-SQL scenarios above (tests 1–20, several covering more than one required category) passed. All real-n8n-execution scenarios listed passed after the two n8n-specific bugs found during this testing (each-item-mode return shape; `.first()` not being paired-item-aware) were fixed and re-verified.

## Clean re-import results

- **Official CLI export/import**: exported via `n8n export:workflow`, sanitized (zero nodes carry a `credentials` key — verified programmatically), imported into a second, genuinely fresh n8n instance.
- **Second clean PostgreSQL instance**: a fresh `initdb` cluster (different port, different database) with the schema and all ten functions installed from scratch via the exact DDL in [Schema](#schema). The multi-page pull-sync scenario and the outbox create scenario were re-run against this second database — identical results (`pages: 2, applied: 3`; `claimed: 1, applied: 1`).
- **Execution-data persistence**: this workflow's settings disable execution persistence (`saveManualExecutions: false`, `saveDataSuccessExecution/ErrorExecution: 'none'`) — confirmed the committed file carries these settings. Functional testing above required a temporary, uncommitted copy with persistence enabled specifically to inspect results; the committed file was never altered for this.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; single input field, `syncCalendarId`.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope note; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used **twenty** times: input validation, page-state initialization and accumulation, fetch/finalize classification, and eight nodes running in "Run Once for Each Item" mode for per-item outbox classification and request building (see [Feasibility investigation](#feasibility-investigation) for the two n8n-specific behaviors of that mode this design had to account for).
- **IF** (`n8n-nodes-base.if`, v2.2) — used **nine** times, gating every mutation and every loop-continuation decision structurally: input validity, lease acquisition, HTTP success, expired-token detection, page-apply success, final-page detection, whether there's outbox work at all, create-vs-mutate routing, and idempotency-adopt-vs-create routing.
- **Merge** (`n8n-nodes-base.merge`, v3, append mode) — used **two** times, to reconverge the create/mutate outbox branches before a shared finalize step, and to reconverge the "no outbox work" path with the finalized-work path before the final summary.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used **eight** times, `Execute Query` operation, every query fully parameterized. Each call is a single, self-contained function invocation (see [Schema](#schema)) rather than an inline multi-statement query — this repository's [`whatsapp-appointment-confirmation-cancellation`](whatsapp-appointment-confirmation-cancellation.md#root-cause-correction) documents in detail why a same-snapshot, multi-CTE inline statement is not a safe substitute for this pattern under concurrency.
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

The ten functions this workflow calls, in the exact form verified during testing:

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
BEGIN
  IF p_outcome = 'success' THEN
    RETURN QUERY SELECT * FROM sync_outbox_finalize_success(
      p_outbox_id, p_calendar_id, p_owner, p_appointment_id, p_operation, p_google_event_id, p_etag, p_updated
    );
  ELSIF p_outcome = 'conflict' THEN
    RETURN QUERY SELECT (SELECT out_ok FROM sync_outbox_finalize_conflict(
      p_outbox_id, p_calendar_id, p_owner, p_appointment_id, p_google_event_id, p_reason, p_details
    )), p_reason;
  ELSE
    RETURN QUERY SELECT (SELECT out_ok FROM sync_outbox_finalize_failure(
      p_outbox_id, p_calendar_id, p_owner, p_reason
    )), p_reason;
  END IF;
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
BEGIN
  PERFORM 1 FROM sync_calendars sc
    WHERE sc.id = p_calendar_id AND sc.lease_owner = p_owner AND sc.lease_expires_at >= now();
  IF NOT FOUND THEN
    RETURN QUERY SELECT false, 'lease_not_owned'::TEXT;
    RETURN;
  END IF;

  UPDATE sync_outbox o
  SET status = 'applied', google_event_id = p_google_event_id, updated_at = now()
  WHERE o.id = p_outbox_id;

  IF p_operation = 'cancel' THEN
    UPDATE appointment_calendar_mappings m
    SET sync_status = 'calendar_cancelled', etag = p_etag, last_calendar_updated = p_updated::TIMESTAMPTZ,
        last_seen_at = now(), updated_at = now()
    WHERE m.appointment_id = p_appointment_id AND m.calendar_id = p_calendar_id;
  ELSE
    INSERT INTO appointment_calendar_mappings
      (appointment_id, calendar_id, google_event_id, etag, mapping_version, sync_status, last_calendar_updated, last_seen_at)
    VALUES (p_appointment_id, p_calendar_id, p_google_event_id, p_etag, 1, 'synced', p_updated::TIMESTAMPTZ, now())
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
  v_updated TEXT;
  v_appt_id TEXT;
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

  FOR v_item IN SELECT jsonb_array_elements(p_items) LOOP
    v_event_id := v_item->>'id';
    v_status := v_item->>'status';
    v_etag := v_item->>'etag';
    v_updated := v_item->>'updated';
    v_appt_id := v_item#>>'{extendedProperties,private,appointmentId}';

    IF v_event_id IS NULL OR v_event_id = '' THEN
      v_malformed := v_malformed + 1;
      INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
      VALUES (NULL, p_calendar_id, NULL, 'malformed_event', v_item);
      CONTINUE;
    END IF;

    IF v_appt_id IS NULL OR v_appt_id = '' THEN
      v_unknown := v_unknown + 1;
      INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
      VALUES (NULL, p_calendar_id, v_event_id, 'unknown_event', v_item);
      CONTINUE;
    END IF;

    SELECT * INTO v_existing FROM appointment_calendar_mappings m
      WHERE m.calendar_id = p_calendar_id AND m.google_event_id = v_event_id;

    IF NOT FOUND THEN
      -- No local mapping row for this (calendar_id, google_event_id) pair
      -- yet. Only ever adopt it if the marker's appointmentId resolves to
      -- a REAL, known Postgres appointment -- Postgres decides identity,
      -- never the incoming Calendar payload alone -- and that appointment
      -- doesn't already have a mapping elsewhere (which would make this a
      -- duplicate-claim conflict, not a fresh discovery).
      SELECT * INTO v_appt_row FROM appointments a WHERE a.appointment_id = v_appt_id;
      IF NOT FOUND THEN
        v_unknown := v_unknown + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (v_appt_id, p_calendar_id, v_event_id, 'unknown_appointment_reference', v_item);
        CONTINUE;
      END IF;

      SELECT * INTO v_other_mapping FROM appointment_calendar_mappings m2 WHERE m2.appointment_id = v_appt_id;
      IF FOUND THEN
        v_conflict := v_conflict + 1;
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (v_appt_id, p_calendar_id, v_event_id, 'duplicate_mapping_candidate', v_item);
        CONTINUE;
      END IF;

      IF v_status = 'cancelled' THEN
        -- A pre-existing, already-cancelled event for a known appointment
        -- with no mapping yet: record the tombstoned mapping directly,
        -- no need to round-trip through "synced" first.
        v_tombstoned := v_tombstoned + 1;
        INSERT INTO appointment_calendar_mappings
          (appointment_id, calendar_id, google_event_id, etag, mapping_version, sync_status, last_calendar_updated, last_seen_at)
        VALUES (v_appt_id, p_calendar_id, v_event_id, v_etag, 1, 'calendar_cancelled', v_updated::TIMESTAMPTZ, now());
        CONTINUE;
      END IF;

      v_applied := v_applied + 1;
      INSERT INTO appointment_calendar_mappings
        (appointment_id, calendar_id, google_event_id, etag, mapping_version, sync_status, last_calendar_updated, last_seen_at)
      VALUES (v_appt_id, p_calendar_id, v_event_id, v_etag, 1, 'synced', v_updated::TIMESTAMPTZ, now());
      CONTINUE;
    END IF;

    IF v_existing.appointment_id <> v_appt_id THEN
      v_conflict := v_conflict + 1;
      INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
      VALUES (v_existing.appointment_id, p_calendar_id, v_event_id, 'appointment_id_mismatch', v_item);
      UPDATE appointment_calendar_mappings m2 SET last_seen_at = now()
        WHERE m2.appointment_id = v_existing.appointment_id AND m2.calendar_id = p_calendar_id;
      CONTINUE;
    END IF;

    IF v_status = 'cancelled' THEN
      v_tombstoned := v_tombstoned + 1;
      IF v_existing.sync_status <> 'calendar_cancelled' THEN
        INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
        VALUES (v_appt_id, p_calendar_id, v_event_id, 'calendar_side_cancellation', v_item);
      END IF;
      UPDATE appointment_calendar_mappings m3
      SET sync_status = 'calendar_cancelled', etag = v_etag, last_calendar_updated = v_updated::TIMESTAMPTZ,
          last_seen_at = now(), updated_at = now()
      WHERE m3.appointment_id = v_appt_id AND m3.calendar_id = p_calendar_id;
      CONTINUE;
    END IF;

    IF v_existing.etag IS NOT NULL AND v_existing.etag <> v_etag AND v_existing.sync_status = 'synced' THEN
      v_conflict := v_conflict + 1;
      INSERT INTO sync_conflicts (appointment_id, calendar_id, google_event_id, reason, details)
      VALUES (v_appt_id, p_calendar_id, v_event_id, 'concurrent_edit', v_item);
      UPDATE appointment_calendar_mappings m4
      SET sync_status = 'conflict', etag = v_etag, last_calendar_updated = v_updated::TIMESTAMPTZ,
          last_seen_at = now(), updated_at = now()
      WHERE m4.appointment_id = v_appt_id AND m4.calendar_id = p_calendar_id;
      CONTINUE;
    END IF;

    v_applied := v_applied + 1;
    UPDATE appointment_calendar_mappings m5
    SET etag = v_etag, last_calendar_updated = v_updated::TIMESTAMPTZ,
        last_seen_at = now(), updated_at = now()
    WHERE m5.appointment_id = v_appt_id AND m5.calendar_id = p_calendar_id;
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

Every function follows the same pattern established by this repository's other Postgres-backed workflows: `RETURNS TABLE` columns are prefixed (`out_...`) to avoid ambiguity with real table column names inside the function body (a bug class documented and fixed in [`whatsapp-appointment-confirmation-cancellation`](whatsapp-appointment-confirmation-cancellation.md)), and every function always returns exactly one row (or, for the batch-claim function, zero-to-N rows by design, handled on the n8n side via `alwaysOutputData` — see [Feasibility investigation](#feasibility-investigation)).

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
- This workflow has been verified as a template against the specific n8n, Node.js, and PostgreSQL versions documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads `syncCalendarId` from its caller — never a Calendar id or event id, both of which are read exclusively from Postgres. Reads and writes `appointment_calendar_mappings`, `sync_calendars` (cursor/lease state), `sync_outbox` (drains, never enqueues), and `sync_conflicts`. Makes outbound HTTP calls only to the fixed Google Calendar API host, only for calendars and events already validated against Postgres state. Its final summary output contains only aggregate counts (`pull.pages/applied/tombstoned/unknown/malformed/conflict`, `outbox.claimed/applied/conflicted/failed`) and status — never customer message text, calendar event bodies, credentials, or raw Postgres rows.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-20
