# WhatsApp Appointment Confirmation and Cancellation

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook) that acts on an **already-classified** reply action from [`whatsapp-appointment-reply-parser`](whatsapp-appointment-reply-parser.md): `confirmed`, `cancelled`, `reschedule_requested`, or `manual_review`. It does not classify any reply text itself — that logic exists only in the reply parser. This workflow's job starts *after* classification: durably recording the decision in Postgres (the source of truth), and — only for `confirmed`/`cancelled` — updating the linked Google Calendar event.

It accepts `replyEventId` (a durable idempotency key), `appointmentId`, `action`, `expectedVersion` (optimistic concurrency), `calendarEventId`, and `actionTimestamp`. Every request is validated strictly, recorded exactly once even if the same `replyEventId` is retried or arrives concurrently, and never allowed to silently overwrite a newer appointment state.

**Postgres and Google Calendar cannot be joined into one atomic transaction.** A calendar failure *after* a successful, durable database write is reported honestly as `calendar_sync_failed` — never as false success, and the database write is never rolled back to "fix" it. See [Failure-state design](#failure-state-design).

## Real business use case

Once a customer replies to an appointment message and [`whatsapp-appointment-reply-parser`](whatsapp-appointment-reply-parser.md) classifies that reply, *something* has to actually act on it: record the decision durably, and keep the calendar in sync. This workflow is that acting step — the reply parser only tells you what the customer said; this workflow is what changes the appointment's real state.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22), against an isolated **PostgreSQL 16.15** instance for testing.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the six-field input contract.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used nine times: input validation, four controlled-response builders, update-result classification, calendar-request building, and calendar-result classification.
- **IF** (`n8n-nodes-base.if`, v2.3) — used five times, gating every mutation structurally: input validity, idempotency reservation, optimistic-update success, whether the action requires a calendar call, and calendar-call success.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used six times, `Execute Query` operation, every query fully parameterized (`$1, $2, ...` placeholders with a separate values array — never string-built SQL). See [Database and idempotency design](#database-and-idempotency-design).
- **HTTP Request** (`n8n-nodes-base.httpRequest`, v4.5) — the single outbound call to the Google Calendar API, reached only for `confirmed`/`cancelled` actions after the database write already succeeded. Configured with `Never Error` + `Full Response` + `onError: continueErrorOutput` (the same pattern [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) uses), so both HTTP-status failures and transport-level failures (timeouts, connection errors) route to the same controlled classification instead of crashing the execution.

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature.

## Feasibility investigation

Every mechanism below was verified experimentally against a live n8n v2.35.4 instance and an isolated local PostgreSQL 16.15 instance before being used — see [Test procedure](#test-procedure) for exactly how.

- **Parameterized Postgres queries.** Confirmed by reading n8n's own Postgres node source: the `Execute Query` operation's `$1, $2, ...` placeholders are filled from a separate `values` array passed to the underlying driver — genuine parameterization, not string concatenation. Every query in this workflow uses this mechanism; none build SQL by concatenating input values into the query string.
- **Zero-row Postgres results produce zero n8n items — this is not optional to handle.** Verified experimentally: when a query's result set has zero rows, the Postgres node outputs zero items, and a zero-item input to any downstream node (IF nodes included) means that node **does not execute at all** for that path — it does not take a "false" branch, it simply doesn't run. Every query in this workflow that could legitimately return zero matching rows (idempotency reservation on conflict, optimistic update on a stale version or missing appointment) is written to **always return exactly one row**, using scalar subqueries (e.g. `(SELECT count(*) FROM ...)::int`), so every branch decision is an explicit, testable IF condition instead.
- **Safe optimistic-concurrency handling.** Verified directly against Postgres: `UPDATE ... WHERE appointment_id = $1 AND version = $2` is itself the atomic compare-and-set — a second concurrent attempt with the same stale `expectedVersion` re-reads the already-updated row via Postgres's row-level locking and its `WHERE` clause no longer matches, so it safely updates zero rows rather than overwriting. Verified under genuine concurrency (5 simultaneous requests with the same `replyEventId` against the same appointment): exactly 1 succeeded, 4 were correctly reported as `duplicate`, and exactly 1 calendar call was made.
- **Durable idempotency via `replyEventId`.** Verified via a `UNIQUE` constraint on `reply_events.reply_event_id` and an `INSERT ... ON CONFLICT (reply_event_id) DO NOTHING` reservation step that runs *before* any appointment mutation or calendar call — a duplicate `replyEventId` is detected atomically at the database level, not via an application-level race-prone check-then-act.
- **Google Calendar node behavior.** The native `n8n-nodes-base.googleCalendar` node is tightly bound to its own OAuth2 flow and does not offer a way to redirect its requests to a different host for testing — unlike the fixed-host, mockable design already used by `whatsapp-template-message-sender`. Instead, this workflow uses an **HTTP Request** node targeting the Google Calendar REST API directly, authenticated via `predefinedCredentialType: googleCalendarOAuth2Api` (n8n's own registered credential type for this exact purpose, confirmed to support generic HTTP Request node usage since it extends n8n's generic OAuth2 credential base). This gives the same mockability the sender workflow already established: a temporary, uncommitted test copy can point the same node at a local mock server using a synthetic Header Auth credential instead, without changing the workflow's logic at all.
- **Credential-reference sanitization.** Confirmed the committed workflow contains no credential IDs of any kind for either the Postgres or the Calendar nodes — both have no `credentials` key at all after sanitization, identical to the established pattern in every other workflow in this repository.
- **Export/import portability.** Confirmed via the official `n8n export:workflow` / `import:workflow` CLI that the committed workflow's `settings`, `nodes`, and `connections` survive a clean round trip byte-for-byte identical, on a second, genuinely separate n8n instance.
- **Failure behavior when Postgres is unavailable.** Verified directly: with Postgres stopped, the very first Postgres node ("Reserve Idempotency") throws, and — since this workflow does not configure `continueOnFail`/`continueErrorOutput` for its Postgres nodes — the entire execution fails with an error. This is **deliberate**: Postgres is this workflow's source of truth; if it cannot be reached, this workflow cannot safely determine idempotency or current appointment state, so failing loudly (visible in n8n's execution log, and propagated to the calling workflow as a failed sub-workflow execution) is safer than fabricating a plausible-looking but meaningless status. Confirmed zero calendar calls occur in this case. The caller is responsible for detecting the failure and deciding whether/when to retry — this workflow itself never retries automatically.
- **Failure behavior when Google Calendar is unavailable.** Handled explicitly and gracefully (unlike the Postgres case) — see [Failure-state design](#failure-state-design). By this point in the workflow, the database write has already durably succeeded, so a calendar outage is recorded as `calendar_sync_failed`, not allowed to crash or roll anything back.

**Feasibility verdict: GO.** All of the above were verified experimentally; no fabricated workflow content was used.

## Database and idempotency design

**Postgres is the single source of truth for appointment state.** The minimal required schema:

```sql
CREATE TABLE appointments (
  appointment_id       TEXT PRIMARY KEY,
  status                TEXT NOT NULL DEFAULT 'scheduled',
  calendar_event_id     TEXT,
  version                INTEGER NOT NULL DEFAULT 1,
  calendar_sync_status   TEXT NOT NULL DEFAULT 'in_sync',
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reply_events (
  reply_event_id   TEXT PRIMARY KEY,
  appointment_id    TEXT NOT NULL,
  action            TEXT NOT NULL,
  result_status     TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- **`appointments.version`** is the optimistic-concurrency column. Every accepted action increments it; the caller must supply the version it last observed as `expectedVersion`, and the update only applies if that still matches — see [Feasibility investigation](#feasibility-investigation) for how this was verified safe under real concurrency.
- **`reply_events.reply_event_id UNIQUE`** (the primary key) is the idempotency constraint. This table is not just a log — the `INSERT ... ON CONFLICT DO NOTHING` against this constraint is the actual mechanism that makes processing a given `replyEventId` at-most-once, atomically, at the database level.
- **`appointments.calendar_sync_status`** (`in_sync` / `calendar_sync_failed`) tracks whether the calendar is known to reflect the database's current state — see [Failure-state design](#failure-state-design).

**Processing sequence for every valid request**, all via parameterized queries:

1. **Reserve idempotency**: `INSERT INTO reply_events (...) VALUES (...) ON CONFLICT (reply_event_id) DO NOTHING RETURNING reply_event_id`, wrapped so the query always returns exactly one row reporting whether the reservation succeeded. If not (a duplicate `replyEventId`), processing stops here — `duplicate`, zero further mutations of any kind.
2. **Optimistic update**: `UPDATE appointments SET status = $action, version = version + 1, ... WHERE appointment_id = $1 AND version = $2`, again wrapped to always return one row reporting whether it applied, and — if not — whether the appointment exists at all (`missing_appointment`) or exists with a different version (`conflict`). This step applies uniformly to all four actions; `reschedule_requested` and `manual_review` are recorded here too, just like `confirmed`/`cancelled` — they simply never proceed to a calendar call afterward.
3. Only for `confirmed`/`cancelled`: the calendar call (see [Calendar integration design](#calendar-integration-design)), followed by a final Postgres statement that records both the calendar-sync outcome and the reply event's final `result_status` together, in one statement.

## Calendar integration design

A single outbound `PATCH` request to the Google Calendar v3 API, only for `confirmed`/`cancelled`, only after the database write already succeeded:

- **Host is fixed**: `https://www.googleapis.com` — like the Meta Graph API host in `whatsapp-template-message-sender`, this is a constant inside the workflow, never taken from input, so a caller cannot redirect this workflow's outbound request anywhere else.
- **Calendar is fixed to `primary`** — the authenticated account's own default calendar. This workflow does not accept a caller-supplied calendar ID; supporting multiple calendars would require adding that as a validated input.
- **`confirmed`** → `PATCH /calendar/v3/calendars/primary/events/{calendarEventId}` with `{"extendedProperties":{"private":{"confirmationStatus":"confirmed"}}}` — updates the event's private metadata without altering its visible schedule.
- **`cancelled`** → the same endpoint with `{"status":"cancelled"}` — Google's documented way to cancel an event via the API while leaving it in place (auditable), rather than `DELETE`, which would remove it entirely.
- **Authentication**: `predefinedCredentialType` using n8n's own `googleCalendarOAuth2Api` credential type — you create this credential yourself (your own Google Cloud OAuth2 app, consented against your own calendar) exactly as you would for n8n's native Google Calendar node; this workflow does not include or require any credential.

## Failure-state design

Postgres and Google Calendar are two separate systems; no mechanism joins a Postgres transaction and a Google API call into one atomic unit. This workflow does not pretend otherwise:

1. The database write (the appointment's `confirmed`/`cancelled` state, and its `version` increment) is committed **durably and first**, before any calendar call is attempted.
2. If the calendar call then fails — any non-2xx HTTP status, or a transport-level failure like a timeout — the appointment's `calendar_sync_status` is set to `calendar_sync_failed`, and the returned `status` is `calendar_sync_failed`, **never** `confirmed` or `cancelled`. A caller cannot mistake this for full success.
3. **The database write is never rolled back** to "fix" a calendar failure. Rolling back would silently discard a customer's already-durable confirmation or cancellation decision — worse than a flagged, visible sync gap that a human or a separate reconciliation process can resolve.
4. **No automatic retry** of the calendar call, anywhere in this workflow — confirmed by test (exactly one calendar request recorded per failing scenario). Retrying calendar mutations automatically risks duplicate calendar side effects; that decision is left to whoever calls this workflow.

Verified directly, for every calendar failure mode tested (400, 401, 404, 429, 500, timeout): the database showed the correct `confirmed`/`cancelled` status and incremented `version`, with `calendar_sync_status = 'calendar_sync_failed'` — the customer's decision was never lost, and the sync gap was never hidden.

## Required credentials

**Two**, neither included in the exported JSON — both nodes have no credential bound after import, by design:

| Credential | Bound to node(s) | Type |
|---|---|---|
| e.g. "Appointments Postgres" | `Reserve Idempotency`, `Apply Optimistic Update`, `Record Non-Update Outcome`, `Record No-Calendar Outcome`, `Mark In Sync`, `Mark Calendar Sync Failed` | n8n **Postgres** credential (`postgres`), pointed at your own database with the schema above |
| e.g. "Calendar Access" | `Call Calendar API` | n8n **Google Calendar OAuth2 API** credential (`googleCalendarOAuth2Api`) — set up your own Google Cloud OAuth2 app exactly as you would for n8n's native Google Calendar node |

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Setup steps

1. Create the `appointments` and `reply_events` tables (see [Database and idempotency design](#database-and-idempotency-design)) in your own Postgres database, and populate `appointments` from your real booking data however you already do that — this workflow does not create or seed appointment records itself.
2. Import `whatsapp-appointment-confirmation-cancellation.json`.
3. Create and bind your Postgres credential (see [Required credentials](#required-credentials)) to all six Postgres nodes.
4. Create and bind your Google Calendar OAuth2 API credential to the `Call Calendar API` node.
5. Build whatever calls this sub-workflow — typically the same orchestration that calls `whatsapp-appointment-reply-parser`, passing its classified `action` straight through, plus `appointmentId`/`calendarEventId`/`expectedVersion` looked up from your own appointment records (this workflow does not resolve "which appointment is this WhatsApp conversation about" — that mapping is outside its scope), a fresh `replyEventId` per inbound reply event, and the real `actionTimestamp`.
6. Test with synthetic data against your own isolated setup first — see [Test procedure](#test-procedure) for how this package itself was tested.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database) against an isolated local **PostgreSQL 16.15** instance (a fresh `initdb` cluster under a temporary directory, listening only on `127.0.0.1` on a non-default port, populated only with synthetic seed data — no existing/system Postgres installation or any real customer database was touched). **The real Google Calendar API was never contacted** — this project does not have a dedicated synthetic test calendar/credential available, so per this repository's testing policy, calendar behavior was tested only through a temporary, uncommitted mock-bound copy.

**1. Timing/routing/validation logic, and every scenario where zero calendar calls are required — tested end-to-end against the actual shipped workflow**, bound to the real, isolated test Postgres instance: rejection, duplicate, conflict, and missing-appointment scenarios are, by construction, blocked by IF nodes before the calendar HTTP node ever runs, so these were safe to execute against the real committed file. Confirmed via each execution's recorded node history that `Call Calendar API` never ran for any of these cases.

**2. Calendar HTTP-status handling — tested only through a temporary, uncommitted mock-bound copy**: a temporary duplicate of this workflow, identical except its `Call Calendar API` node targeted a local mock HTTP server (`http://127.0.0.1:8766`, Python standard library, not part of this repository) via a synthetic Header Auth credential instead of `googleCalendarOAuth2Api`, and its Execute Workflow Trigger accepted one additional test-only `mockStatus` field used only to steer the mock server's response. This copy was never exported or committed.

| # | Test | Result | Verified via |
|---|---|---|---|
| 1 | Valid confirmation | `confirmed`, `httpStatus: 200` | mock-bound copy |
| 2 | Valid cancellation | `cancelled`, `httpStatus: 200` | mock-bound copy |
| 3 | Reschedule request | `reschedule_requested`, zero calendar calls | real committed file |
| 4 | Manual review | `manual_review`, zero calendar calls | real committed file |
| 5 | Duplicate `replyEventId` | Second call: `duplicate`; exactly 1 calendar call total (from the first) | mock-bound copy |
| 6 | Repeated confirmation (stale version after the first succeeds) | Second call: `conflict`, zero additional calendar calls | mock-bound copy |
| 7 | Repeated cancellation (same pattern) | Second call: `conflict`, zero additional calendar calls | mock-bound copy |
| 8 | Stale `expectedVersion` | `conflict`, zero calendar calls | real committed file |
| 9 | Missing appointment | `missing_appointment`, zero calendar calls | real committed file |
| 10 | Missing `calendarEventId` (for a calendar-requiring action) | `rejected`, zero calendar calls | real committed file |
| 11 | Invalid `action` | `rejected`, zero calendar calls | real committed file |
| 12 | Oversized/invalid-character/injection-shaped identifiers | `rejected`, identifier(s) returned as `null` (never echoed unbounded) | real committed file |
| 13 | Impossible `actionTimestamp` (e.g. February 30) | `rejected`, zero calendar calls | real committed file |
| 14 | Postgres unavailable | Execution fails with an error at the first Postgres node; zero calendar calls | real committed file, Postgres stopped |
| 15 | Calendar success | `confirmed`, `httpStatus: 200` | mock-bound copy |
| 16 | Calendar HTTP 400 | `calendar_sync_failed`, `httpStatus: 400` | mock-bound copy |
| 17 | Calendar HTTP 401 | `calendar_sync_failed`, `httpStatus: 401` | mock-bound copy |
| 18 | Calendar HTTP 404 | `calendar_sync_failed`, `httpStatus: 404` | mock-bound copy |
| 19 | Calendar HTTP 429 | `calendar_sync_failed`, `httpStatus: 429` | mock-bound copy |
| 20 | Calendar HTTP 500 | `calendar_sync_failed`, `httpStatus: 500` | mock-bound copy |
| 21 | Calendar timeout | `calendar_sync_failed`, `httpStatus: null` | mock-bound copy |
| 22 | Database succeeds but calendar fails | Confirmed directly in Postgres: `appointments.status` correctly `confirmed`/`cancelled` with an incremented `version`, `calendar_sync_status = 'calendar_sync_failed'` | mock-bound copy |
| 23 | No automatic retry | Exactly one request recorded by the mock server's own request log for a failing scenario | mock-bound copy |
| 24 | Concurrent duplicate requests | 5 simultaneous requests, same `replyEventId`: exactly 1 `confirmed`, 4 `duplicate`, exactly 1 calendar call | mock-bound copy |
| 25 | Controlled output contains no sensitive fields | Confirmed programmatically: no response contains message text, phone numbers, credentials, SQL, calendar payloads, or raw provider errors | real committed file + mock-bound copy |
| 26 | Execution-data persistence settings verified | Canary `replyEventId` immediately unreachable via the executions API for this workflow's own execution; confirmed physically present but soft-deleted in SQLite immediately after; confirmed physically purged following a process restart under accelerated pruning | real committed file |
| 27 | Official CLI export/import | Exported via `n8n export:workflow`, imported into a clean instance, exported again — `settings`, `nodes`, `connections` byte-for-byte identical | real committed file |
| 28 | Complete rerun on a second clean instance | A genuinely separate, freshly-initialized n8n instance plus a freshly-created Postgres database reproduced every applicable result above identically | real committed file (cases 3, 4, 8, 9, 10, 11, 12, 13, 14) + a fresh mock-bound copy built on that same clean instance (remaining cases) |

All test data was synthetic: fake appointment/calendar-event/reply-event identifiers, a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, synthetic `CANARY_...`-labeled identifiers for persistence testing, and a local mock server — no real Postgres database, Google Calendar, credentials, or customer data anywhere. A repository-wide scan for every canary/test value used in this testing found none committed to any file.

## Known limitations

- **Does not resolve "which appointment is this reply about."** `appointmentId` must already be known to whatever calls this workflow — mapping an inbound WhatsApp conversation to an appointment record is outside this workflow's scope, same as `whatsapp-appointment-reminder`'s scope boundary with respect to reading a calendar.
- **`reschedule_requested` never invents a new appointment time.** It only records that a reschedule was requested, for a human or a separate downstream scheduling workflow to act on.
- **A compromised Postgres or Google Calendar credential defeats this workflow's own guarantees entirely** — the optimistic-concurrency and idempotency mechanisms protect against races and duplicate processing, not against a credential that shouldn't have been trusted in the first place.
- **No automatic retries, intentionally, anywhere** — a failed calendar call is reported as `calendar_sync_failed` and left for the caller or a separate reconciliation process to resolve, exactly like the sender workflow's own no-retry design.
- **Execution-data persistence is disabled by this workflow's settings, but physical deletion is asynchronous and bounded, not instantaneous** — see [Test procedure](#test-procedure) case 26, and the fuller explanation in the webhook security gateway's documentation.
- **Single default calendar (`primary`) only** — this workflow does not support routing different appointments to different calendars.
- **The real Google Calendar API was never contacted during testing** — calendar HTTP-status handling was verified only through a temporary, uncommitted mock-bound copy, disclosed precisely above. Verify calendar behavior against your own real, non-production calendar before relying on it.
- This workflow has been verified as a template against the specific n8n, Node.js, and PostgreSQL versions and test suite documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads `replyEventId`, `appointmentId`, `action`, `expectedVersion`, `calendarEventId`, and `actionTimestamp` from its caller. Writes to your Postgres database: the appointment's status/version/calendar-sync-status, and a durable record of the reply event and its outcome. Makes at most one outbound HTTP call, to the Google Calendar API, only for `confirmed`/`cancelled` actions after the database write succeeds. Its controlled output contains only `status`, `replyEventId`, `appointmentId`, `version`, and `httpStatus` — never customer message text, a phone number, credentials, SQL details, calendar event payloads, or raw provider errors.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
