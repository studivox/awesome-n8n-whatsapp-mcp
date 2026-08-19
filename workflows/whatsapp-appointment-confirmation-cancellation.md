# WhatsApp Appointment Confirmation and Cancellation

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook) that acts on an **already-classified** reply action from [`whatsapp-appointment-reply-parser`](whatsapp-appointment-reply-parser.md): `confirmed`, `cancelled`, `reschedule_requested`, or `manual_review`. It does not classify any reply text itself — that logic exists only in the reply parser. This workflow's job starts *after* classification: durably recording the decision in Postgres (the source of truth), and — only for `confirmed`/`cancelled` — updating the linked Google Calendar event.

It accepts `replyEventId` (a durable idempotency key), `appointmentId`, `action`, `expectedVersion` (optimistic concurrency), and `actionTimestamp`. **`calendarEventId` is not a caller input.** It is read exclusively from Postgres (`appointments.calendar_event_id`) — arbitrary caller-supplied data can never redirect the Calendar request to a different event than the one this appointment actually owns.

Every request is validated strictly, recorded exactly once even if the same `replyEventId` is retried or arrives concurrently, and never allowed to silently overwrite a newer appointment state.

**Postgres and Google Calendar cannot be joined into one atomic transaction.** A calendar failure *after* a successful, durable database write is reported honestly as `calendar_sync_failed` — never as false success, and the database write is never rolled back to "fix" it. An appointment with a Calendar mutation in flight (`calendar_sync_pending`) rejects overlapping actions (`calendar_busy`) rather than risking a second, overlapping Calendar request. See [Pending and reconciliation behavior](#pending-and-reconciliation-behavior).

## Real business use case

Once a customer replies to an appointment message and [`whatsapp-appointment-reply-parser`](whatsapp-appointment-reply-parser.md) classifies that reply, *something* has to actually act on it: record the decision durably, and keep the calendar in sync. This workflow is that acting step — the reply parser only tells you what the customer said; this workflow is what changes the appointment's real state.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22), against an isolated **PostgreSQL 16.15** instance for testing.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the five-field input contract (no `calendarEventId`).
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used ten times: input validation, reserve-result classification, three controlled-response builders, three finalize-input builders, calendar-request building, calendar-result classification, and the final finalize-response builder.
- **IF** (`n8n-nodes-base.if`, v2.3) — used five times, gating every mutation structurally: input validity, duplicate detection, whether a calendar call is needed, whether a `calendarEventId` is present, and calendar-call success.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used **three** times, `Execute Query` operation, every query fully parameterized (`$1, $2, ...` placeholders with a separate values array — never string-built SQL). See [Atomic idempotency/state-machine design](#atomic-idempotencystate-machine-design).
- **HTTP Request** (`n8n-nodes-base.httpRequest`, v4.5) — the single outbound call to the Google Calendar API, reached only for `confirmed`/`cancelled` actions after the database write already durably recorded `calendar_sync_pending`. Configured with `Never Error` + `Full Response` + `onError: continueErrorOutput` (the same pattern [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) uses), so both HTTP-status failures and transport-level failures (timeouts, connection errors) route to the same controlled classification instead of crashing the execution.

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature.

## Feasibility investigation

Every mechanism below was verified experimentally against a live n8n v2.35.4 instance and an isolated local PostgreSQL 16.15 instance before being used — see [Test procedure](#test-procedure) for exactly how.

- **Parameterized Postgres queries.** Confirmed by reading n8n's own Postgres node source: the `Execute Query` operation's `$1, $2, ...` placeholders are filled from a separate `values` array passed to the underlying driver — genuine parameterization, not string concatenation.
- **Zero-row Postgres results produce zero n8n items — this is not optional to handle.** Verified experimentally: when a query's result set has zero rows, the Postgres node outputs zero items, and a zero-item input to any downstream node (IF nodes included) means that node **does not execute at all** for that path. Every query in this workflow that could legitimately match zero rows is written to **always return exactly one row**, using scalar subqueries, so every branch decision is an explicit, testable IF condition instead.
- **Safe optimistic-concurrency handling.** Verified directly against Postgres: `UPDATE ... WHERE appointment_id = $1 AND version = $2` is itself the atomic compare-and-set. Verified under genuine concurrency (two different `replyEventId`s, same `expectedVersion`, fired simultaneously against the same appointment): exactly one succeeded, the other correctly received `conflict`, and exactly one calendar call was made.
- **A single atomic statement can safely combine idempotency reservation with the appointment compare-and-set.** This was the central redesign in this correction — see [Atomic idempotency/state-machine design](#atomic-idempotencystate-machine-design) for what was wrong with the original two-statement design and how the fix was verified.
- **Google Calendar node behavior.** The native `n8n-nodes-base.googleCalendar` node is tightly bound to its own OAuth2 flow and does not offer a way to redirect its requests to a different host for testing. Instead, this workflow uses an **HTTP Request** node targeting the Google Calendar REST API directly, authenticated via `predefinedCredentialType: googleCalendarOAuth2Api` (n8n's own registered credential type for this exact purpose). This gives the same mockability the sender workflow already established.
- **Credential-reference sanitization.** Confirmed the committed workflow contains no credential IDs of any kind — no node has a `credentials` key at all after sanitization.
- **Export/import portability.** Confirmed via the official `n8n export:workflow` / `import:workflow` CLI that the committed workflow's `settings`, `nodes`, and `connections` survive a clean round trip byte-for-byte identical, on a second, genuinely separate n8n instance.
- **Failure behavior when Postgres is unavailable — at every point in the workflow, not just the first.** Verified directly at **both** places this workflow writes to Postgres: (1) with Postgres stopped entirely, the atomic `Reserve And Apply` node throws immediately, zero calendar calls occur; (2) with Postgres reachable for that first write but deliberately unreachable for the *second* write (`Finalize Calendar Result`, by binding it to a broken connection in an isolated test), the calendar call had already gone out (and, in the cases tested, already succeeded or failed on Google's side), but the local finalize write throws — the appointment durably remains `calendar_sync_pending`, is never falsely marked as either `confirmed`/`cancelled` (success) or `calendar_sync_failed`, and no automatic retry of the calendar call is attempted. See [Pending and reconciliation behavior](#pending-and-reconciliation-behavior).

**Feasibility verdict: GO.** All of the above were verified experimentally; no fabricated workflow content was used.

## Atomic idempotency/state-machine design

**What was wrong before this correction:** the original design used two separate Postgres statements — an idempotency-reservation `INSERT` first, then a separate optimistic-update `UPDATE` in a following node. If the process crashed, or Postgres became unreachable, *between* those two statements, the reservation had already committed with a generic `result_status = 'processing'`, but the appointment itself was never updated. Every retry of that `replyEventId` would then hit the reservation's `ON CONFLICT` and be reported as a plain `duplicate` — permanently stranding the event with no way to tell what, if anything, actually happened.

**The fix: idempotency reservation and the appointment compare-and-set now happen in ONE atomic Postgres statement.** A single `WITH ... UPDATE ... RETURNING ... INSERT ... SELECT ...` query does all of the following as one indivisible unit — either the whole statement commits, or none of it does:

```sql
WITH appt_check AS (
  SELECT appointment_id, version, calendar_event_id, calendar_sync_status
  FROM appointments WHERE appointment_id = $2
),
updated AS (
  UPDATE appointments
  SET status = $3,
      version = version + 1,
      calendar_sync_status = CASE
        WHEN $3 IN ('confirmed','cancelled') THEN 'calendar_sync_pending'
        ELSE 'not_applicable'
      END,
      updated_at = now()
  WHERE appointment_id = $2
    AND version = $5
    AND calendar_sync_status <> 'calendar_sync_pending'
    AND NOT EXISTS (SELECT 1 FROM reply_events WHERE reply_event_id = $1)
  RETURNING appointment_id, calendar_event_id, version, status, calendar_sync_status
),
reserve AS (
  INSERT INTO reply_events (reply_event_id, appointment_id, action, action_timestamp, result_status)
  SELECT $1, $2, $3, $4::timestamptz,
    CASE
      WHEN EXISTS (SELECT 1 FROM updated) THEN
        CASE WHEN $3 IN ('confirmed','cancelled') THEN 'calendar_sync_pending' ELSE $3 END
      WHEN NOT EXISTS (SELECT 1 FROM appt_check) THEN 'missing_appointment'
      WHEN (SELECT calendar_sync_status FROM appt_check) = 'calendar_sync_pending'
           AND (SELECT version FROM appt_check) = $5 THEN 'calendar_busy'
      ELSE 'conflict'
    END
  WHERE NOT EXISTS (SELECT 1 FROM reply_events WHERE reply_event_id = $1)
  ON CONFLICT (reply_event_id) DO NOTHING
  RETURNING reply_event_id, result_status
)
SELECT
  (SELECT count(*) FROM reserve)::int AS reserved_count,
  (SELECT result_status FROM reserve) AS new_result_status,
  (SELECT result_status FROM reply_events WHERE reply_event_id = $1) AS existing_result_status,
  (SELECT calendar_event_id FROM updated) AS calendar_event_id,
  (SELECT version FROM updated) AS new_version;
```

**Why this closes the crash gap:** there is no possible interruption point that leaves reservation committed but the appointment mutation not — they are the same statement. The *only* durable intermediate state this workflow can leave an appointment in is the deliberate, explicit `calendar_sync_pending` — recorded atomically alongside the appointment's own status/version change, not as an accident of a crash.

**Duplicate handling now returns the real, existing outcome — never a generic label.** When `reserved_count = 0` (the `ON CONFLICT` fired), the query still reports the *pre-existing* `result_status` for that `replyEventId` — `calendar_sync_pending`, `calendar_sync_failed`, `confirmed`, `conflict`, whatever it actually is. A pending event, a failed event, and a genuinely completed event are all distinguishable to the caller, instead of everything collapsing into one `"duplicate"` string.

**The `calendar_sync_status <> 'calendar_sync_pending'` guard prevents overlapping Calendar mutations.** If an appointment already has a Calendar call in flight, a *different* `replyEventId` attempting a new action against it (even with a correct `expectedVersion`) is rejected as `calendar_busy` — zero additional Calendar calls, appointment left untouched — until the pending action resolves. See [Pending and reconciliation behavior](#pending-and-reconciliation-behavior).

Verified experimentally, directly against Postgres and through the live workflow: fresh reservation, duplicate-with-existing-status, and `calendar_busy` all produce exactly the rows and query results this design predicts — see [Test procedure](#test-procedure).

## Pending and reconciliation behavior

The complete state machine this workflow can leave an appointment/reply-event pair in:

| State | Meaning |
|---|---|
| *(no reply_events row)* | This `replyEventId` was never durably reserved — either rejected before the atomic write, or the atomic write never ran (e.g. Postgres was unavailable). |
| `missing_appointment` | The atomic write ran; no appointment with this id exists. |
| `conflict` | The atomic write ran; `expectedVersion` did not match the appointment's current version. |
| `calendar_busy` | The atomic write ran; the appointment already has a Calendar mutation in flight for a *different* reply event. |
| `reschedule_requested` / `manual_review` | Finalized atomically in the same write as the appointment mutation — no Calendar state is ever entered for these two actions. |
| `calendar_sync_pending` | The atomic write committed the appointment's new status/version **and** this durable pending marker, before any Calendar call was attempted. This is the only state a crash between the database write and the Calendar call (or between the Calendar call and finalizing its result) can leave visible. |
| `missing_calendar_event` | Reached `calendar_sync_pending`, but `appointments.calendar_event_id` was null — zero Calendar calls made; finalized via the version-guarded write. |
| `calendar_sync_failed` | The Calendar call was attempted and failed (any non-2xx status or a transport failure), finalized via the version-guarded write. |
| `confirmed` / `cancelled` | The Calendar call succeeded and was finalized via the version-guarded write — full success. |
| `reconciliation_required` | The version-guarded finalize write's guard did not match (see [Version-guard correction](#version-guard-correction)) — appointments was left untouched; a human or a separate reconciliation workflow must resolve this by hand. |

**If the process crashes after the atomic database write commits but before or during the Calendar request**, the database durably and visibly shows `calendar_sync_pending` — confirmed directly in Postgres. A duplicate request for the same `replyEventId` reports this exact pending state and makes zero new Calendar calls (the `reply_events` reservation already exists). A *different* `replyEventId` for the same appointment is rejected as `calendar_busy`, also making zero Calendar calls.

**If the Calendar call itself succeeds or fails but the final Postgres write then fails** (e.g. Postgres becomes unreachable at exactly that moment), this workflow does **not** automatically retry the Calendar call — the previous request may already have reached Google, and retrying blindly risks a duplicate mutation. The execution fails loudly (no `continueOnFail` on the finalize node), and the appointment is left showing `calendar_sync_pending` — its state from the initial atomic write — until a human or a separate reconciliation process checks Google's actual event state and updates Postgres by hand. Verified directly for both outcomes (Calendar success and Calendar failure) by isolating the finalize write against a deliberately unreachable Postgres connection: in both cases, exactly one Calendar call was made, and the appointment remained durably `calendar_sync_pending`.

**Resolving a stuck `calendar_sync_pending` or `reconciliation_required` row is outside this workflow's scope.** It requires checking the actual event state on Google's side and manually updating `appointments.calendar_sync_status` (and, if appropriate, `reply_events.result_status`) — this workflow deliberately does not attempt that automatically.

## Version-guard correction

The two Postgres write-nodes downstream of the initial atomic write (`Finalize Calendar Result`, used for the missing-calendar-event, Calendar-success, and Calendar-failure paths alike) apply their write with a **version guard**:

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

`$2` is the exact `version` the original atomic write produced for *this* reply event. If a slow execution's Calendar call finally resolves after a *newer* action has already been accepted for the same appointment (bumping its version further), this guard does not match — `appointments` is left completely untouched (the newer row is never overwritten), and `reply_events.result_status` for this event is set to `reconciliation_required` instead of falsely claiming success or failure. Verified directly against Postgres: a finalize attempt using a stale version left the appointment's actual (newer) row unchanged and correctly recorded `reconciliation_required`.

## Calendar transition correction

The `confirmed` Calendar request body was corrected to restore an event previously marked `cancelled`, not just flag private metadata:

- **`confirmed`** → `PATCH /calendar/v3/calendars/primary/events/{calendarEventId}` with `{"status":"confirmed","extendedProperties":{"private":{"confirmationStatus":"confirmed"}}}` — sets the event's actual `status` field back to `confirmed`, so a customer confirming after having previously cancelled visibly un-cancels the calendar entry, not just a private flag nobody but this workflow reads.
- **`cancelled`** → the same endpoint with `{"status":"cancelled"}` — Google's documented way to cancel an event via the API while leaving it in place (auditable), rather than `DELETE`.

Verified directly through a `confirmed → cancelled → confirmed` sequence against the temporary mock-bound copy: the mock event's own recorded `status` field genuinely went `confirmed` → `cancelled` → `confirmed`, proving the second confirmation actually restores it rather than merely re-flagging an event the mock (and, by the same logic, a real calendar) would still show as cancelled.

## actionTimestamp persistence

`actionTimestamp` is strictly validated (see [Feasibility investigation](#feasibility-investigation) and the reminder workflow's identical component-level ISO-8601 validation) but was previously discarded after validation — an unused, security-significant input is a red flag in itself, and discarding it also meant no record existed of when the customer's decision was actually made.

**Correction:** `reply_events.action_timestamp TIMESTAMPTZ NOT NULL` is now part of the schema, and the validated value is persisted as a genuine parameterized value (`$4::timestamptz`) in the same atomic write that reserves idempotency. Verified directly in Postgres: a `Z`-suffixed timestamp and an explicit `+03:00`-offset timestamp representing the same instant both normalize to the identical stored UTC instant, and an impossible timestamp (e.g. `2026-02-30T10:00:00Z`) is still rejected before ever reaching Postgres.

## Required credentials

**Two**, neither included in the exported JSON — no node has a credential bound after import, by design:

| Credential | Bound to node(s) | Type |
|---|---|---|
| e.g. "Appointments Postgres" | `Reserve And Apply`, `Finalize Calendar Result` | n8n **Postgres** credential (`postgres`), pointed at your own database with the schema in [Atomic idempotency/state-machine design](#atomic-idempotencystate-machine-design) |
| e.g. "Calendar Access" | `Call Calendar API` | n8n **Google Calendar OAuth2 API** credential (`googleCalendarOAuth2Api`) — set up your own Google Cloud OAuth2 app exactly as you would for n8n's native Google Calendar node |

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Setup steps

1. Create the `appointments` and `reply_events` tables in your own Postgres database — full schema:
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
   ```
   Populate `appointments` (including `calendar_event_id`) from your real booking data however you already do that — this workflow does not create or seed appointment records itself.
2. Import `whatsapp-appointment-confirmation-cancellation.json`.
3. Create and bind your Postgres credential (see [Required credentials](#required-credentials)) to `Reserve And Apply` and `Finalize Calendar Result`.
4. Create and bind your Google Calendar OAuth2 API credential to the `Call Calendar API` node.
5. Build whatever calls this sub-workflow — typically the same orchestration that calls `whatsapp-appointment-reply-parser`, passing its classified `action` straight through, plus `appointmentId`/`expectedVersion` looked up from your own appointment records (this workflow does not resolve "which appointment is this WhatsApp conversation about" — that mapping is outside its scope), a fresh `replyEventId` per inbound reply event, and the real `actionTimestamp`. **Do not pass a calendar event id** — it is intentionally not part of this workflow's input contract.
6. Have a plan for resolving `calendar_sync_pending`/`reconciliation_required` rows that don't clear on their own — see [Pending and reconciliation behavior](#pending-and-reconciliation-behavior). This workflow does not do this automatically.
7. Test with synthetic data against your own isolated setup first — see [Test procedure](#test-procedure) for how this package itself was tested.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database) against an isolated local **PostgreSQL 16.15** instance (a fresh `initdb` cluster under a temporary directory, listening only on `127.0.0.1` on a non-default port, populated only with synthetic seed data — no existing/system Postgres installation or any real customer database was touched). **The real Google Calendar API was never contacted** — this project does not have a dedicated synthetic test calendar/credential available, so per this repository's testing policy, calendar behavior was tested only through a temporary, uncommitted mock-bound copy.

**Paths where zero Calendar calls are structurally required — tested end-to-end against the real committed file, bound to the isolated test Postgres instance.** **Paths requiring an actual Calendar call — tested only through a temporary, uncommitted mock-bound copy** (identical to the committed file except `Call Calendar API` targets a local mock HTTP server via a synthetic Header Auth credential instead of `googleCalendarOAuth2Api`, and the Execute Workflow Trigger accepts one additional test-only `mockStatus` field). This copy was never exported or committed.

| # | Test | Result | Verified via |
|---|---|---|---|
| 1 | Caller cannot supply or override `calendarEventId` | A request with an extra, attacker-controlled `calendarEventId` field is ignored entirely — not a declared input, never read | real committed file (field simply has no effect) |
| 2 | Database `calendar_event_id` used exclusively | The actual Calendar request used only the id stored in Postgres for that appointment | real committed file |
| 3 | Missing database `calendar_event_id` | `missing_calendar_event`, zero Calendar calls | real committed file |
| 4 | Crash/failure before the atomic DB transition | An input that fails validation makes zero Postgres calls at all — no orphan reservation possible, since reservation only happens inside the atomic statement | real committed file |
| 5 | Crash immediately after the atomic DB transition | Confirmed directly in Postgres: `calendar_sync_pending` is durable and visible immediately after the atomic write, before any Calendar call | real committed file, direct SQL inspection |
| 6 | Duplicate while pending | `calendar_busy` (a different `replyEventId`) / the existing `calendar_sync_pending` status (the same `replyEventId`) — zero additional Calendar calls either way | mock-bound copy |
| 7 | Duplicate after success | Existing `confirmed`/`cancelled` result returned, zero additional calls | mock-bound copy |
| 8 | Duplicate after Calendar failure | Existing `calendar_sync_failed` result returned, zero additional calls | mock-bound copy |
| 9 | Postgres failure during final **success** recording | Isolated by binding `Finalize Calendar Result` to a deliberately unreachable Postgres connection: the Calendar call had already succeeded (1 mock request recorded), but the appointment durably remained `calendar_sync_pending` — never falsely marked `confirmed` | mock-bound copy, targeted Postgres-connection isolation |
| 10 | Postgres failure during final **failure** recording | Same isolation, with the Calendar call returning a 500: the appointment durably remained `calendar_sync_pending` — never falsely marked `calendar_sync_failed` or success | mock-bound copy, targeted Postgres-connection isolation |
| 11 | Final Calendar result with a stale appointment version | Confirmed directly in Postgres: a finalize attempt against an already-superseded version left the newer row completely untouched and recorded `reconciliation_required` | direct SQL inspection against the exact committed query |
| 12 | Concurrent different `replyEventId`s, same `expectedVersion` | Exactly one `confirmed`, the other `conflict`, exactly one Calendar call | mock-bound copy |
| 13 | A second action while Calendar sync is pending | `calendar_busy`, zero additional Calendar calls | mock-bound copy |
| 14 | `confirmed → cancelled` Calendar transition | Both transitions succeed with the correct status | mock-bound copy |
| 15 | `cancelled → confirmed` restores `status: confirmed` | Confirmed via the mock server's own recorded event state: genuinely restored, not merely re-flagged | mock-bound copy |
| 16 | `actionTimestamp` stored and normalized correctly | A `Z` timestamp and an equivalent explicit-offset timestamp both normalize to the identical stored UTC instant in Postgres; an impossible timestamp is still rejected | direct SQL inspection + real committed file |
| 17 | Non-calendar actions (`reschedule_requested`/`manual_review`) remain atomic | Finalized in the same atomic write as the appointment mutation, zero Calendar calls | real committed file |
| 18 | Previous rejection/concurrency/status coverage (missing appointment, stale version, oversized/injection-shaped identifiers returning `null`, full Calendar HTTP-status matrix 400/401/404/429/500/timeout) | All pass, identical to prior verification | real committed file (rejections) + mock-bound copy (Calendar statuses) |
| 19 | Official CLI export/import | Exported via `n8n export:workflow`, imported into a clean instance, exported again — `settings`, `nodes`, `connections` byte-for-byte identical | real committed file |
| 20 | Complete rerun on a second clean n8n/Postgres instance | A genuinely separate, freshly-initialized n8n instance plus a freshly-created Postgres database reproduced the calendar-redirect-prevention, missing-calendar-event, reschedule/manual-review, duplicate, stale-version, missing-appointment, oversized-identifier, Calendar-400, concurrency, and full confirm→cancel→confirm-restore results identically | real committed file (rejections) + a fresh mock-bound copy built on that same clean instance (Calendar-requiring cases) |
| 21 | Execution-data persistence settings verified | Canary `replyEventId` immediately unreachable via the executions API for this workflow's own execution; confirmed physically present but soft-deleted in SQLite immediately after; confirmed physically purged following a process restart under accelerated pruning | real committed file |
| 22 | Controlled output contains no sensitive fields | Confirmed programmatically: no response contains message text, phone numbers, credentials, SQL, calendar payloads, or raw provider errors | real committed file + mock-bound copy |

All test data was synthetic: fake appointment/calendar-event/reply-event identifiers, a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, synthetic `CANARY_...`-labeled identifiers for persistence testing, and a local mock server — no real Postgres database, Google Calendar, credentials, or customer data anywhere. A repository-wide scan for every canary/test value used in this testing found none committed to any file.

**One operational note surfaced during this correction round, unrelated to this workflow's own logic:** on this n8n version, running the official `n8n import:workflow` CLI against a data directory that already has an instance owner configured was observed, in this testing, to clear the owner's stored email — re-submitting `POST /rest/owner/setup` with the same address immediately and idempotently restored access. This is an operational quirk of the CLI tooling itself, not something this workflow's design can control or is affected by at runtime.

## Known limitations

- **Does not resolve "which appointment is this reply about."** `appointmentId` must already be known to whatever calls this workflow — mapping an inbound WhatsApp conversation to an appointment record is outside this workflow's scope.
- **`reschedule_requested` never invents a new appointment time.** It only records that a reschedule was requested, for a human or a separate downstream scheduling workflow to act on.
- **A stuck `calendar_sync_pending` or `reconciliation_required` row requires manual or separate-workflow resolution.** This workflow deliberately never automatically retries a Calendar mutation for a pending event, because a prior request may already have reached Google — see [Pending and reconciliation behavior](#pending-and-reconciliation-behavior).
- **A compromised Postgres or Google Calendar credential defeats this workflow's own guarantees entirely** — the optimistic-concurrency and idempotency mechanisms protect against races and duplicate processing, not against a credential that shouldn't have been trusted in the first place.
- **No automatic retries, intentionally, anywhere.**
- **Execution-data persistence is disabled by this workflow's settings, but physical deletion is asynchronous and bounded, not instantaneous.**
- **Single default calendar (`primary`) only** — this workflow does not support routing different appointments to different calendars.
- **The real Google Calendar API was never contacted during testing** — calendar HTTP-status handling, and the pending/reconciliation Postgres-failure scenarios, were verified only through a temporary, uncommitted mock-bound copy, disclosed precisely above. Verify calendar behavior against your own real, non-production calendar before relying on it.
- This workflow has been verified as a template against the specific n8n, Node.js, and PostgreSQL versions and test suite documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads `replyEventId`, `appointmentId`, `action`, `expectedVersion`, and `actionTimestamp` from its caller — **not** a calendar event id, which is read exclusively from Postgres. Writes to your Postgres database: the appointment's status/version/calendar-sync-status, and a durable record of the reply event (including the validated `action_timestamp`) and its outcome. Makes at most one outbound HTTP call, to the Google Calendar API, only for `confirmed`/`cancelled` actions after the database durably records `calendar_sync_pending`. Its controlled output contains only `status`, `replyEventId`, `appointmentId`, `version`, and `httpStatus` — never customer message text, a phone number, credentials, SQL details, calendar event payloads, or raw provider errors.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
