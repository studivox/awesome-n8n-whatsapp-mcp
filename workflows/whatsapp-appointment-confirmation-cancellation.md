# WhatsApp Appointment Confirmation and Cancellation

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook) that acts on an **already-classified** reply action from [`whatsapp-appointment-reply-parser`](whatsapp-appointment-reply-parser.md): `confirmed`, `cancelled`, `reschedule_requested`, or `manual_review`. It does not classify any reply text itself — that logic exists only in the reply parser. This workflow's job starts *after* classification: durably recording the decision in Postgres (the source of truth), and — only for `confirmed`/`cancelled` — updating the linked Google Calendar event.

It accepts `replyEventId` (a durable idempotency key), `appointmentId`, `action`, `expectedVersion` (optimistic concurrency), and `actionTimestamp`. **`calendarEventId` is not a caller input.** It is read exclusively from Postgres (`appointments.calendar_event_id`) — arbitrary caller-supplied data can never redirect the Calendar request to a different event than the one this appointment actually owns.

Every request is validated strictly, and processed **at most once** per `replyEventId` — including under genuine concurrency, and including when the same `replyEventId` is (mistakenly or maliciously) reused against a *different* appointment, action, or timestamp. See [New transaction/idempotency design](#new-transactionidempotency-design) for exactly what guarantees this and how it was proven under adversarial concurrent testing.

**Postgres and Google Calendar cannot be joined into one atomic transaction.** A calendar failure *after* a successful, durable database write is reported honestly as `calendar_sync_failed` — never as false success, and the database write is never rolled back to "fix" it. An appointment with a Calendar mutation in flight (`calendar_sync_pending`) rejects overlapping actions (`calendar_busy`) rather than risking a second, overlapping Calendar request. See [Pending and reconciliation behavior](#pending-and-reconciliation-behavior).

## Real business use case

Once a customer replies to an appointment message and [`whatsapp-appointment-reply-parser`](whatsapp-appointment-reply-parser.md) classifies that reply, *something* has to actually act on it: record the decision durably, and keep the calendar in sync. This workflow is that acting step — the reply parser only tells you what the customer said; this workflow is what changes the appointment's real state.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22), against an isolated **PostgreSQL 16.15** instance for testing.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the five-field input contract (no `calendarEventId`).
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used ten times: input validation, reserve-result classification, a shared status-response builder, three finalize-input builders, calendar-request building, calendar-result classification, and the final finalize-response builder.
- **IF** (`n8n-nodes-base.if`, v2.3) — used four times, gating every mutation structurally: input validity, whether a calendar call is needed, whether a `calendarEventId` is present, and calendar-call success.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used **two** times, `Execute Query` operation, every query fully parameterized (`$1, $2, ...` placeholders with a separate values array — never string-built SQL). `Reserve And Apply` calls a PL/pgSQL function (see [New transaction/idempotency design](#new-transactionidempotency-design)); `Finalize Calendar Result` runs a version-guarded inline query (see [Version-guard correction](#version-guard-correction), unchanged from the prior correction round).
- **HTTP Request** (`n8n-nodes-base.httpRequest`, v4.5) — the single outbound call to the Google Calendar API, reached only for `confirmed`/`cancelled` actions after the database write already durably recorded `calendar_sync_pending`. Configured with `Never Error` + `Full Response` + `onError: continueErrorOutput` (the same pattern [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) uses), so both HTTP-status failures and transport-level failures (timeouts, connection errors) route to the same controlled classification instead of crashing the execution.

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature.

## Root-cause correction

**A prior correction round replaced a two-statement reserve/update design with a single CTE-based `WITH ... UPDATE ... INSERT ... SELECT` statement, describing it as fully atomic. That claim was wrong**, and the mistake matters enough to state plainly rather than quietly fix: PostgreSQL's data-modifying CTEs within one statement all read against the **same snapshot**, and the relative execution order between sibling CTEs is not something a query author may rely on. The old statement's appointment `UPDATE` guarded itself with `AND NOT EXISTS (SELECT 1 FROM reply_events WHERE reply_event_id = $1)` — a same-snapshot check, not a real ownership gate. Two concurrent requests sharing the same `replyEventId` but targeting **two different `appointmentId`s** would both see "no existing row" for that `replyEventId` (neither had committed yet) and would **both** proceed to update their respective, different appointment rows. The `reply_events` unique constraint would then correctly reject one of the two `INSERT` attempts — but by then the "losing" request's appointment mutation had already happened and would still commit with the rest of that statement, since `ON CONFLICT DO NOTHING` is a no-op, not an abort. The existing concurrency test at the time used the *same* `appointmentId` for both concurrent requests, so the appointment-version compare-and-set masked this defect — the bug only manifests when the shared `replyEventId` targets **different** appointments.

**Verified directly:** the old design, run against real PostgreSQL 16.15 with two concurrent requests sharing one `replyEventId` against two different appointments, mutated **both** appointments. This is a real, exploitable cross-appointment idempotency failure, not a theoretical one.

## New transaction/idempotency design

The fix moves all of the reservation-and-mutation logic into a single **PL/pgSQL function**, invoked by exactly one parameterized Postgres-node call (`SELECT * FROM process_reply_event($1, $2, $3, $4::timestamptz, $5)`), so that ownership of a `replyEventId` is established by real, sequential, exception-safe control flow — not by a same-snapshot check racing against a sibling statement:

```sql
CREATE OR REPLACE FUNCTION process_reply_event(
  p_reply_event_id   TEXT,
  p_appointment_id    TEXT,
  p_action            TEXT,
  p_action_timestamp  TIMESTAMPTZ,
  p_expected_version  INTEGER
) RETURNS TABLE (
  route                 TEXT,  -- 'owner_applied' | 'duplicate_match' | 'idempotency_mismatch'
  result_status          TEXT,
  out_calendar_event_id  TEXT,
  out_new_version         INTEGER
) AS $$
DECLARE
  v_existing reply_events%ROWTYPE;
  v_appt appointments%ROWTYPE;
  v_new_calendar_sync_status TEXT;
  v_result_status TEXT;
BEGIN
  -- Step 1: attempt to OWN the reservation. This INSERT is the single,
  -- indivisible gate for this replyEventId, regardless of which
  -- appointmentId is requested. Postgres's unique index on
  -- reply_event_id guarantees exactly one concurrent caller can ever
  -- complete it -- independent of statement snapshot or sibling-CTE
  -- ordering, because it's a real INSERT with real exception handling,
  -- not a same-snapshot existence check.
  BEGIN
    INSERT INTO reply_events (reply_event_id, appointment_id, action, action_timestamp, result_status)
    VALUES (p_reply_event_id, p_appointment_id, p_action, p_action_timestamp, 'reserved');
  EXCEPTION WHEN unique_violation THEN
    -- We do NOT own it. No appointment of any kind is touched on this
    -- path -- this is the actual fix for the cross-appointment race:
    -- losing this INSERT means the function returns here, before any
    -- appointment is ever read or written.
    SELECT * INTO v_existing FROM reply_events WHERE reply_event_id = p_reply_event_id;

    IF v_existing.appointment_id = p_appointment_id
       AND v_existing.action = p_action
       AND v_existing.action_timestamp = p_action_timestamp THEN
      RETURN QUERY SELECT 'duplicate_match'::TEXT, v_existing.result_status, NULL::TEXT, NULL::INTEGER;
    ELSE
      -- Different appointmentId, action, or actionTimestamp under the
      -- same replyEventId -- never return the existing result as though
      -- it belonged to this (different) request.
      RETURN QUERY SELECT 'idempotency_mismatch'::TEXT, 'idempotency_mismatch'::TEXT, NULL::TEXT, NULL::INTEGER;
    END IF;
    RETURN;
  END;

  -- We own the reservation. Lock the target appointment row before
  -- reading it, so a concurrent owner-applied call for a DIFFERENT
  -- replyEventId against the SAME appointment serializes correctly here.
  SELECT * INTO v_appt FROM appointments WHERE appointment_id = p_appointment_id FOR UPDATE;

  IF NOT FOUND THEN
    UPDATE reply_events SET result_status = 'missing_appointment' WHERE reply_event_id = p_reply_event_id;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'missing_appointment'::TEXT, NULL::TEXT, NULL::INTEGER;
    RETURN;
  END IF;

  IF v_appt.calendar_sync_status = 'calendar_sync_pending' AND v_appt.version = p_expected_version THEN
    UPDATE reply_events SET result_status = 'calendar_busy' WHERE reply_event_id = p_reply_event_id;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'calendar_busy'::TEXT, NULL::TEXT, NULL::INTEGER;
    RETURN;
  END IF;

  IF v_appt.version <> p_expected_version THEN
    UPDATE reply_events SET result_status = 'conflict' WHERE reply_event_id = p_reply_event_id;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'conflict'::TEXT, NULL::TEXT, NULL::INTEGER;
    RETURN;
  END IF;

  -- Version matches and not busy -- apply the state transition.
  v_new_calendar_sync_status := CASE WHEN p_action IN ('confirmed','cancelled') THEN 'calendar_sync_pending' ELSE 'not_applicable' END;
  v_result_status := CASE WHEN p_action IN ('confirmed','cancelled') THEN 'calendar_sync_pending' ELSE p_action END;

  UPDATE appointments
  SET status = p_action, version = version + 1, calendar_sync_status = v_new_calendar_sync_status, updated_at = now()
  WHERE appointment_id = p_appointment_id
  RETURNING version, calendar_event_id INTO v_appt.version, v_appt.calendar_event_id;

  UPDATE reply_events SET result_status = v_result_status WHERE reply_event_id = p_reply_event_id;

  RETURN QUERY SELECT 'owner_applied'::TEXT, v_result_status, v_appt.calendar_event_id, v_appt.version;
END;
$$ LANGUAGE plpgsql;
```

**Why this is safe under concurrent requests, specifically:**

- **The reservation gate is a real `INSERT` with real exception handling**, not a same-snapshot `NOT EXISTS`. PostgreSQL's `EXCEPTION WHEN unique_violation` block is a documented, idiomatic pattern (implemented internally via a subtransaction/savepoint) for exactly this "exactly one concurrent caller may proceed" requirement — it is not something this workflow invented.
- **Appointment reads and writes only ever happen after ownership is established**, by real sequential control flow inside the function body — there is no code path that reads or writes `appointments` before the `INSERT` has genuinely succeeded for *this* call.
- **The whole function call is one statement, hence one transaction.** Any exception anywhere inside it — including after the appointment `UPDATE` has been issued — rolls back everything the function did, including the reservation `INSERT`. There is no partial-completion state reachable from a crash inside the function.
- **`SELECT ... FOR UPDATE`** locks the target appointment row before it's read, so two different, legitimately-owned `replyEventId`s targeting the *same* appointment concurrently still serialize correctly through Postgres's normal row-level locking — this preserves the same optimistic-concurrency guarantee the prior design relied on, just made explicit rather than implicit in an `UPDATE`'s own re-check behavior.

## Request-binding rules

`process_reply_event` binds every `replyEventId` to the exact `appointmentId`, `action`, and `actionTimestamp` it was first reserved with, and enforces this on every subsequent call:

| Situation | Result |
|---|---|
| Same `replyEventId` + identical `appointmentId`/`action`/`actionTimestamp` | `duplicate_match` — the existing, real `result_status` for that reservation is returned. Zero additional appointment mutation, zero additional Calendar call. |
| Same `replyEventId` + **different** `appointmentId` | `idempotency_mismatch` — zero mutation of any appointment, zero Calendar call. The existing reservation's result is never returned as though it belonged to this different appointment. |
| Same `replyEventId` + **different** `action` | `idempotency_mismatch` — same guarantees. |
| Same `replyEventId` + **different** `actionTimestamp` | `idempotency_mismatch`. A strict equality comparison is used deliberately — a customer's reply event genuinely happens at one instant; if a caller supplies a different `actionTimestamp` for what claims to be the same `replyEventId`, that is itself a sign the request isn't actually the same event, and this workflow does not attempt to guess which one is "more correct." |

This was verified directly, both at the SQL level (concurrent `psql` processes calling `process_reply_event` directly) and through the live workflow over HTTP — see [Cross-appointment concurrency results](#cross-appointment-concurrency-results).

## Pending and reconciliation behavior

The complete state machine this workflow can leave an appointment/reply-event pair in:

| State | Meaning |
|---|---|
| *(no reply_events row)* | This `replyEventId` was never durably reserved — either rejected before the atomic call, or the call never ran (e.g. Postgres was unavailable). |
| `idempotency_mismatch` | This `replyEventId` was already reserved by a request with a different `appointmentId`, `action`, or `actionTimestamp`. Zero mutation. |
| `missing_appointment` | The reservation succeeded; no appointment with this id exists. |
| `conflict` | The reservation succeeded; `expectedVersion` did not match the appointment's current version. |
| `calendar_busy` | The reservation succeeded; the appointment already has a Calendar mutation in flight for a *different, already-owned* reply event. |
| `reschedule_requested` / `manual_review` | Finalized atomically inside `process_reply_event` — no Calendar state is ever entered for these two actions. |
| `calendar_sync_pending` | The atomic function call committed the appointment's new status/version **and** this durable pending marker, before any Calendar call was attempted. This is the only state a crash between the database write and the Calendar call (or between the Calendar call and finalizing its result) can leave visible. |
| `missing_calendar_event` | Reached `calendar_sync_pending`, but `appointments.calendar_event_id` was null — zero Calendar calls made; finalized via the version-guarded write. |
| `calendar_sync_failed` | The Calendar call was attempted and failed (any non-2xx status or a transport failure), finalized via the version-guarded write. |
| `confirmed` / `cancelled` | The Calendar call succeeded and was finalized via the version-guarded write — full success. |
| `reconciliation_required` | The version-guarded finalize write's guard did not match (see [Version-guard correction](#version-guard-correction)) — appointments was left untouched; a human or a separate reconciliation workflow must resolve this by hand. |

**If the process crashes after `process_reply_event` commits but before or during the Calendar request**, the database durably and visibly shows `calendar_sync_pending` — confirmed directly in Postgres. A duplicate request for the same `replyEventId` reports this exact pending state and makes zero new Calendar calls. A *different* `replyEventId` for the same appointment is rejected as `calendar_busy`, also making zero Calendar calls.

**If the Calendar call itself succeeds or fails but the final Postgres write then fails** (e.g. Postgres becomes unreachable at exactly that moment), this workflow does **not** automatically retry the Calendar call — the previous request may already have reached Google, and retrying blindly risks a duplicate mutation. The execution fails loudly (no `continueOnFail` on the finalize node), and the appointment is left showing `calendar_sync_pending` — its state from the initial atomic write — until a human or a separate reconciliation process checks Google's actual event state and updates Postgres by hand.

**Resolving a stuck `calendar_sync_pending` or `reconciliation_required` row is outside this workflow's scope.** It requires checking the actual event state on Google's side and manually updating `appointments.calendar_sync_status` (and, if appropriate, `reply_events.result_status`) — this workflow deliberately does not attempt that automatically.

## Version-guard correction

Unchanged from the prior correction round, and unaffected by the root-cause fix above (it only ever runs for the confirmed reservation owner, keyed by that owner's own `replyEventId`, so it was never exposed to the cross-appointment race). The `Finalize Calendar Result` node applies its write with a version guard:

```sql
WITH appt_update AS (
  UPDATE appointments
  SET calendar_sync_status = $4
  WHERE appointment_id = $1 AND version = $2
  RETURNING appointment_id
)
UPDATE reply_events
SET result_status = CASE WHEN EXISTS (SELECT 1 FROM appt_update) THEN $5 ELSE 'reconciliation_required' END
WHERE reply_event_id = $3
RETURNING result_status;
```

`$2` is the exact `version` `process_reply_event` produced for *this* reply event. If a slow execution's Calendar call finally resolves after a *newer* action has already been accepted for the same appointment, this guard does not match — `appointments` is left completely untouched, and `reply_events.result_status` for this event is set to `reconciliation_required` instead of falsely claiming success or failure.

## Calendar transition correction

Unchanged from the prior correction round. The `confirmed` Calendar request body restores an event previously marked `cancelled`, not just a private metadata flag:

- **`confirmed`** → `PATCH /calendar/v3/calendars/primary/events/{calendarEventId}` with `{"status":"confirmed","extendedProperties":{"private":{"confirmationStatus":"confirmed"}}}`.
- **`cancelled`** → the same endpoint with `{"status":"cancelled"}` — Google's documented way to cancel an event via the API while leaving it in place (auditable), rather than `DELETE`.

Re-verified through a `confirmed → cancelled → confirmed` sequence against the temporary mock-bound copy: the mock event's own recorded `status` field genuinely went `confirmed` → `cancelled` → `confirmed`.

## actionTimestamp persistence

Unchanged from the prior correction round. `reply_events.action_timestamp TIMESTAMPTZ NOT NULL` is persisted as a genuine parameterized value (`$4::timestamptz`) by `process_reply_event`. Re-verified: a `Z`-suffixed timestamp and an explicit-offset timestamp representing the same instant both normalize to the identical stored UTC instant; an impossible timestamp is still rejected before ever reaching Postgres.

## Required credentials

**Two**, neither included in the exported JSON — no node has a credential bound after import, by design:

| Credential | Bound to node(s) | Type |
|---|---|---|
| e.g. "Appointments Postgres" | `Reserve And Apply`, `Finalize Calendar Result` | n8n **Postgres** credential (`postgres`), pointed at your own database with the schema and function in [Schema migration](#schema-migration) |
| e.g. "Calendar Access" | `Call Calendar API` | n8n **Google Calendar OAuth2 API** credential (`googleCalendarOAuth2Api`) — set up your own Google Cloud OAuth2 app exactly as you would for n8n's native Google Calendar node |

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Schema migration

**If you already set up the two-table schema from an earlier draft of this workflow** (before this correction), no table structure changed — `appointments` and `reply_events` are identical. The only required migration step is installing the `process_reply_event` function (it did not exist before):

```sql
-- Run this once against your existing database. CREATE OR REPLACE is
-- safe to re-run if you already have an older/broken version installed.
CREATE OR REPLACE FUNCTION process_reply_event( ... ) RETURNS TABLE ( ... ) AS $$ ... $$ LANGUAGE plpgsql;
```

(Full function body in [New transaction/idempotency design](#new-transactionidempotency-design) above.) No `ALTER TABLE` is needed, and no existing `appointments`/`reply_events` data needs to be migrated or rewritten.

**Full schema for a new installation:**

```sql
CREATE TABLE appointments (
  appointment_id       TEXT PRIMARY KEY,
  status                TEXT NOT NULL DEFAULT 'scheduled',
  calendar_event_id     TEXT,
  version                INTEGER NOT NULL DEFAULT 1,
  calendar_sync_status   TEXT NOT NULL DEFAULT 'not_applicable',
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reply_events (
  reply_event_id   TEXT PRIMARY KEY,
  appointment_id    TEXT NOT NULL,
  action            TEXT NOT NULL,
  action_timestamp  TIMESTAMPTZ NOT NULL,
  result_status     TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- process_reply_event(...) -- see New transaction/idempotency design above.
```

`reply_events.reply_event_id` being the `PRIMARY KEY` is what binds every `replyEventId` to its original `appointment_id`/`action`/`action_timestamp` — there is no separate constraint to add; the function's own logic (comparing the stored row against the incoming request) is what enforces the [Request-binding rules](#request-binding-rules) above.

## Setup steps

1. Create the schema — see [Schema migration](#schema-migration) for a new installation, or the migration note there if you already have the two tables from an earlier draft. Populate `appointments` (including `calendar_event_id`) from your real booking data however you already do that — this workflow does not create or seed appointment records itself.
2. Import `whatsapp-appointment-confirmation-cancellation.json`.
3. Create and bind your Postgres credential (see [Required credentials](#required-credentials)) to `Reserve And Apply` and `Finalize Calendar Result`.
4. Create and bind your Google Calendar OAuth2 API credential to the `Call Calendar API` node.
5. Build whatever calls this sub-workflow — typically the same orchestration that calls `whatsapp-appointment-reply-parser`, passing its classified `action` straight through, plus `appointmentId`/`expectedVersion` looked up from your own appointment records (this workflow does not resolve "which appointment is this WhatsApp conversation about" — that mapping is outside its scope), a fresh `replyEventId` per inbound reply event, and the real `actionTimestamp`. **Do not pass a calendar event id** — it is intentionally not part of this workflow's input contract. **Do not reuse a `replyEventId` for a genuinely different reply event** — see [Request-binding rules](#request-binding-rules) for what happens if you do (an explicit `idempotency_mismatch`, not silent corruption, but also not processed).
6. Have a plan for resolving `calendar_sync_pending`/`reconciliation_required` rows that don't clear on their own — see [Pending and reconciliation behavior](#pending-and-reconciliation-behavior). This workflow does not do this automatically.
7. Test with synthetic data against your own isolated setup first — see [Cross-appointment concurrency results](#cross-appointment-concurrency-results) and [Crash-consistency results](#crash-consistency-results) for how this package itself was tested.

## Test procedure

### Cross-appointment concurrency results

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database) against an isolated local **PostgreSQL 16.15** instance (a fresh `initdb` cluster under a temporary directory, listening only on `127.0.0.1` on a non-default port, populated only with synthetic seed data — no existing/system Postgres installation or any real customer database was touched). **The real Google Calendar API was never contacted** — this project does not have a dedicated synthetic test calendar/credential available, so per this repository's testing policy, calendar behavior was tested only through a temporary, uncommitted mock-bound copy.

**Paths where zero Calendar calls are structurally required — tested end-to-end against the real committed file, bound to the isolated test Postgres instance. Paths requiring an actual Calendar call — tested only through a temporary, uncommitted mock-bound copy** (identical to the committed file except `Call Calendar API` targets a local mock HTTP server via a synthetic Header Auth credential instead of `googleCalendarOAuth2Api`, and the Execute Workflow Trigger accepts one additional test-only `mockStatus` field). This copy was never exported or committed.

| # | Test | Result | Verified via |
|---|---|---|---|
| 1 | Same `replyEventId`, same `appointmentId`, identical request, sequential replay | Second call returns the existing `confirmed` result; exactly one Calendar call total | mock-bound copy |
| 2 | Same `replyEventId`, same `appointmentId`, identical request, concurrent replay | Exactly one Calendar call; the durable end state is `confirmed`, version incremented exactly once (the non-owner's transiently-observed status may honestly be `calendar_sync_pending` or `confirmed` depending on timing — both are real, non-fabricated reads, not a bug) | mock-bound copy |
| 3 | Same `replyEventId`, two **different** `appointmentId`s, concurrent — the exact scenario the root-cause bug failed on | Exactly one appointment mutated (`confirmed`, version incremented); the other **completely untouched**; the loser reported `idempotency_mismatch`; exactly one Calendar call | mock-bound copy, direct SQL inspection of both appointment rows |
| 4 | Repeat of #3, 50 randomized iterations with fresh appointment pairs, called directly against `process_reply_event` via concurrent `psql` processes | **50/50 iterations correct**: exactly one appointment confirmed, the other appointment's row byte-for-byte unchanged, exactly one `reply_events` row, every iteration | direct SQL inspection, real PostgreSQL 16.15, repeated against both the original and a second freshly-created database |
| 5 | Same `replyEventId`, same `appointmentId`, conflicting **actions** (`confirmed` vs `cancelled`) concurrently | One action wins and applies; the other reports `idempotency_mismatch` (different `action` under the same `replyEventId`); exactly one Calendar call | mock-bound copy |
| 6 | Same `replyEventId`, different `appointmentId` **and** different `action` | First request applies; second reports `idempotency_mismatch`; exactly one Calendar call | mock-bound copy |
| 7 | Same `replyEventId`, changed `actionTimestamp` | First request applies; second (different timestamp) reports `idempotency_mismatch`; exactly one Calendar call | mock-bound copy |
| 8 | Different `replyEventId`s against the **same** appointment/version, concurrently | Exactly one succeeds (its intended action applied); the other correctly receives `conflict` (the version already moved); exactly one Calendar call | mock-bound copy |
| 9 | Different `replyEventId`s against **different** appointments, concurrently | Both succeed independently, fully correctly, zero interference | real committed file (both actions were `reschedule_requested`/`manual_review`, zero Calendar calls) |
| 15 | At most one appointment changes for a globally duplicated `replyEventId` | Confirmed under 3 fully concurrent identical requests: exactly one version increment, durable end state `confirmed` | mock-bound copy, direct SQL inspection |
| 16 | At most one Calendar call for a globally duplicated `replyEventId` | Same test as #15: exactly one Calendar request recorded regardless of how many concurrent identical requests were made | mock-bound copy |

All of the above except #4's 50-iteration run were also re-run through the live workflow over HTTP (not just direct SQL), confirming the n8n-level wiring (parameterization, response building) matches the underlying function's guarantees.

### Crash-consistency results

| # | Test | Result | Verified via |
|---|---|---|---|
| 10 | Crash/failure immediately after reservation ownership is established | A test-only variant of `process_reply_event` that raises an exception immediately after its own reservation `INSERT` succeeds, called directly: **zero rows left behind** in either `reply_events` or `appointments` — the whole transaction, including the reservation itself, rolled back. No orphan reservation is reachable. | direct SQL inspection, test-only function variant, never committed |
| 11 | Crash/failure during appointment mutation | A test-only variant that raises an exception immediately after issuing the appointment `UPDATE` (but before finalizing `reply_events` or committing): **the appointment row was completely unchanged** (original status, original version) and **no `reply_events` row existed** — the in-flight `UPDATE` was rolled back along with everything else. | direct SQL inspection, test-only function variant, never committed |
| 12 | PostgreSQL unavailable before the operation | With Postgres stopped entirely, the `Reserve And Apply` node throws immediately (~0.15s); zero Calendar calls occur | real committed file, Postgres stopped |
| 13 | PostgreSQL unavailable during finalize | Isolated by binding `Finalize Calendar Result` to a deliberately unreachable Postgres connection: the Calendar call had already succeeded (1 mock request recorded), but the appointment durably remained `calendar_sync_pending` — never falsely marked `confirmed` | mock-bound copy, targeted Postgres-connection isolation |
| 17 | No stranded generic "processing" state can be mistaken for success | `result_status = 'reserved'` (the function's own momentary internal marker) is **never externally visible** — every return path either finalizes a real terminal/pending status within the same transaction, or rolls back entirely on failure (tests #10/#11). Confirmed directly: zero `reply_events` rows with `result_status = 'processing'` or `'reserved'` exist after any test run in this suite. | direct SQL inspection across the full test run |
| 18 | Every durable intermediate state has a documented reconciliation path | See the state table in [Pending and reconciliation behavior](#pending-and-reconciliation-behavior) — every state that is not immediately terminal (`calendar_sync_pending`, `reconciliation_required`) has an explicit, documented manual-resolution path; none are silently unresolvable | documentation review against the actual state machine implemented |

### Regression coverage retained from prior correction rounds

All previously-verified behavior was re-confirmed against the corrected design: missing appointment, stale `expectedVersion`, oversized/injection-shaped identifiers returning `null` (never echoed unbounded), impossible `actionTimestamp` rejected, the full Calendar HTTP-status matrix (400/401/404/429/500/timeout) each producing `calendar_sync_failed` with the correct `httpStatus`, `calendarEventId` caller input having zero effect (verified with an explicit attacker-controlled value present in the request), `confirmed → cancelled → confirmed` genuinely restoring the mock calendar event's status, and controlled output containing no sensitive fields.

### Clean re-import results

- **Official CLI export/import:** exported the corrected workflow via `n8n export:workflow`, sanitized (no credential references of any kind — verified programmatically, zero nodes carry a `credentials` key), imported into a second, genuinely fresh n8n instance, exported again — `settings`, `nodes`, `connections` byte-for-byte identical.
- **Second clean n8n instance plus a freshly-created Postgres database** (schema + function installed fresh): re-ran the cross-appointment race test (#3), the missing-calendar-event test, the reschedule/manual-review zero-Calendar-call test, the `calendarEventId`-input-ignored test, a Calendar-400 test, and the duplicate-returns-existing-status test — all identical results. Additionally re-ran the full 50-iteration cross-appointment adversarial test directly against this second database's `process_reply_event` function — **50/50 correct**.
- **Persistence:** re-confirmed on this corrected build with a canary `replyEventId`: immediately unreachable via the executions API for this workflow's own execution; confirmed physically present but soft-deleted in SQLite immediately after; confirmed physically purged following a process restart under accelerated pruning.

## Known limitations

- **Does not resolve "which appointment is this reply about."** `appointmentId` must already be known to whatever calls this workflow.
- **`reschedule_requested` never invents a new appointment time.**
- **A stuck `calendar_sync_pending` or `reconciliation_required` row requires manual or separate-workflow resolution** — this workflow deliberately never automatically retries a Calendar mutation for a pending event, because a prior request may already have reached Google.
- **`idempotency_mismatch` does not resolve the conflict for you.** If you get this status, it means the `replyEventId` you supplied was already used for a genuinely different request; you must generate a new `replyEventId` for the request that should actually be processed.
- **A compromised Postgres or Google Calendar credential defeats this workflow's own guarantees entirely.**
- **No automatic retries, intentionally, anywhere.**
- **Execution-data persistence is disabled by this workflow's settings, but physical deletion is asynchronous and bounded, not instantaneous.**
- **Single default calendar (`primary`) only.**
- **The real Google Calendar API was never contacted during testing** — calendar HTTP-status handling, and the pending/reconciliation Postgres-failure scenarios, were verified only through a temporary, uncommitted mock-bound copy, disclosed precisely above. Verify calendar behavior against your own real, non-production calendar before relying on it.
- This workflow has been verified as a template against the specific n8n, Node.js, and PostgreSQL versions and test suite documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads `replyEventId`, `appointmentId`, `action`, `expectedVersion`, and `actionTimestamp` from its caller — **not** a calendar event id, which is read exclusively from Postgres. Writes to your Postgres database: the appointment's status/version/calendar-sync-status, and a durable record of the reply event (including the validated `action_timestamp`) and its outcome, bound immutably to the exact `appointmentId`/`action`/`actionTimestamp` it was first reserved with. Makes at most one outbound HTTP call, to the Google Calendar API, only for `confirmed`/`cancelled` actions after the database durably records `calendar_sync_pending`. Its controlled output contains only `status`, `replyEventId`, `appointmentId`, `version`, and `httpStatus` — never customer message text, a phone number, credentials, SQL details, calendar event payloads, or raw provider errors.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
