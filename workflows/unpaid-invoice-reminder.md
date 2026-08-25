# Unpaid Invoice Reminder

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook, not a scheduler) that attempts a single WhatsApp reminder for a single invoice/reminder-stage pair, and durably records the outcome in Postgres. It does not decide *when* a reminder should be attempted, or *which* stage (a first-notice reminder vs. a final-notice reminder) is appropriate — that cadence/policy decision belongs to whatever calls this workflow. This workflow's job starts *after* that decision: verifying from Postgres that the invoice is genuinely still eligible (unpaid, overdue, not already reminded at this stage), reserving exactly one attempt, and — only when genuinely eligible — calling the existing [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) sub-workflow to actually send it.

It accepts `idempotencyKey` (a durable idempotency key), `invoiceId`, `reminderStage`, and `expectedVersion` (optimistic concurrency). **`recipientPhone`, `amount`, `currency`, and `dueDate` are not caller inputs.** They are read exclusively from Postgres (`invoices`) — arbitrary caller-supplied data can never redirect a reminder to a different recipient, fabricate an amount, or misrepresent a due date. `reminderStage` is strictly allowlisted to a small, fixed set of values (`stage1`/`stage2`/`final`) and only ever selects a trusted, hardcoded template/language pair — it never carries arbitrary template content.

Every request is validated strictly, recorded exactly once even if the same `idempotencyKey` is retried or arrives concurrently, and never allowed to silently overwrite a newer invoice state.

**Postgres and the WhatsApp Business Cloud API cannot be joined into one atomic transaction.** A send failure *after* a successful, durable database reservation is reported honestly as `reminder_failed` — never as false success, and the reservation is never rolled back to "fix" it. If the database write that would record a *successful* send itself fails, the reminder is left `reminder_pending`/`reconciliation_required` rather than falsely reported as sent or automatically resent — this workflow does not retry a send that may have already reached WhatsApp.

## Real business use case

An unpaid, overdue invoice needs a nudge — but sending it twice for the same overdue period looks careless, and sending it to the wrong recipient or for the wrong amount (because a caller could influence those values) is a real business and compliance risk. This workflow is the "is this genuinely still worth reminding about, and if so send it exactly once" enforcement step — whatever decides *that a reminder attempt should happen* (a scheduled job scanning overdue invoices, a finance-team action) calls this workflow once per candidate attempt; this workflow is what actually, safely, changes anything.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22), against an isolated **PostgreSQL 16.15** instance for testing.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the four-field input contract (no recipient, amount, currency, or due date).
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used seven times: input validation, reserve-result classification, the status-response builder, the send-request builder, send-result classification, and the final finalize-response builder — plus the rejected-input response builder.
- **IF** (`n8n-nodes-base.if`, v2.3) — used twice: input validity, and whether a send is actually needed.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used twice (`Reserve And Apply`, `Finalize Send Result`), `Execute Query` operation, every query fully parameterized (`$1, $2, ...` placeholders with a separate values array — never string-built SQL). See [Atomic idempotency design](#atomic-idempotency-design).
- **Execute Workflow** (`n8n-nodes-base.executeWorkflow`, v1.2) — the single call to the existing [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) sub-workflow, referenced by its stable, committed workflow id (`R1QDUW9jYqxREyDS`) — the exact same pattern [`whatsapp-appointment-reminder`](whatsapp-appointment-reminder.md) already uses and already proved portable. See [Sender sub-workflow binding](#sender-sub-workflow-binding).

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature.

## Feasibility investigation

Every mechanism below was verified experimentally against a live n8n v2.35.4 instance and an isolated local PostgreSQL 16.15 instance before being used.

- **Calling the existing sender by its committed workflow id, without copying its HTTP/template logic, is portable.** [`whatsapp-appointment-reminder`](whatsapp-appointment-reminder.md) already established and empirically verified this exact mechanism (see its own [Sender sub-workflow binding design](whatsapp-appointment-reminder.md#sender-sub-workflow-binding-design)): an Execute Workflow node with `source: "database"` and `workflowId: {"mode": "id", "value": "R1QDUW9jYqxREyDS"}` resolves correctly because the sender's committed JSON has always shipped with that exact top-level `id` baked in, and the official `n8n import:workflow` CLI preserves a workflow's committed `id` unchanged on import. Re-confirmed directly for this workflow: imported the real, unmodified sender alongside this workflow into a fresh instance, and the reference resolved without any manual rebinding.
- **This reference survives the official CLI export/import round trip.** Re-confirmed on a second, genuinely separate clean n8n instance plus a freshly-created Postgres database: exported this workflow, imported it into the fresh instance, and the sender-by-id reference still resolved without manual intervention. See [Clean re-import results](#clean-re-import-results).
- **A minimal Postgres-backed state model needs exactly three tables**, not one — see [Atomic idempotency design](#atomic-idempotency-design) for why a single invoice-state table isn't enough once a single invoice can have multiple independent reminder stages.
- **The eligibility decision, optimistic-concurrency check, and reminder reservation can be performed atomically in a single PL/pgSQL function**, using the same exception-safe ownership-gate pattern `process_reply_event` uses in [`whatsapp-appointment-confirmation-cancellation`](whatsapp-appointment-confirmation-cancellation.md#atomic-idempotencystate-machine-design) — a real `INSERT` with a real `EXCEPTION WHEN unique_violation` handler, not a same-snapshot `CTE` existence check (that repository's own documented root-cause correction explains exactly why the latter is unsafe under concurrency). Verified directly: two genuinely concurrent identical requests for the same `idempotencyKey` produce exactly one owner and one correctly-reported duplicate, never a `NULL` or stale status for the loser.
- **Crash and Postgres-failure representation matches the confirmation/cancellation workflow's already-proven approach**: a crash between reservation and the sender call leaves the durable, explicit `reminder_pending` state (never an ambiguous intermediate); a Postgres failure *after* a successful send is never silently converted into false success or an automatic resend — see [Crash and reconciliation behavior](#crash-and-reconciliation-behavior).
- **Feasibility verdict: GO.** All of the above were verified experimentally; no fabricated workflow content was used.

## Input and trust-boundary design

The caller supplies exactly four fields: `idempotencyKey`, `invoiceId`, `reminderStage`, `expectedVersion`. Every other value this workflow uses is either read from Postgres or is trusted, hardcoded configuration:

| Value | Source | Why |
|---|---|---|
| `recipientPhone`, `amount`, `currency`, `dueDate`, invoice `status` | `invoices` table, read back from the atomic reservation call | A caller that could set these could redirect a reminder to a different phone number, fabricate an amount, or misrepresent whether an invoice is actually overdue |
| `templateName`, `languageCode` | A small, hardcoded map keyed by the (strictly allowlisted) `reminderStage` value, in `Build Send Request` | "Reminder policy" — which WhatsApp template a given stage uses — is trusted configuration, not something a caller should be able to choose arbitrarily |
| `graphApiVersion`, `phoneNumberId` | Hardcoded constants in `Build Send Request` | Deployment-level WhatsApp Business configuration, not per-request caller input — see [Setup steps](#setup-steps) for what to replace before real use |
| "now" (for the overdue check) | Postgres's own `now()`, evaluated inside `process_invoice_reminder` | No caller-controlled clock — a caller cannot claim an invoice is overdue by supplying a fabricated timestamp |

`reminderStage` itself is validated against an exact allowlist (`stage1` \| `stage2` \| `final`) in `Validate Input` — never an arbitrary string — both because it selects trusted template configuration downstream and because it's part of what an `idempotencyKey` is immutably bound to (see below).

## Atomic idempotency design

**Three tables**, not two, because a single invoice can have multiple independent reminder stages, each needing its own in-flight/completed tracking — a single "is this invoice's reminder in flight" flag on the invoice row itself couldn't represent "stage 1 already sent, stage 2 still eligible" at the same time.

```sql
CREATE TABLE invoices (
  invoice_id      TEXT PRIMARY KEY,
  status          TEXT NOT NULL DEFAULT 'unpaid',   -- 'unpaid' | 'paid' | 'cancelled' | 'disputed'
  recipient_phone TEXT NOT NULL,
  amount          TEXT NOT NULL,                     -- pre-formatted display amount, e.g. '129.00'
  currency        TEXT NOT NULL,                     -- e.g. 'EUR'
  due_date        TIMESTAMPTZ NOT NULL,
  version         INTEGER NOT NULL DEFAULT 1,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE invoice_reminder_state (
  invoice_id          TEXT NOT NULL,
  reminder_stage      TEXT NOT NULL,
  status              TEXT NOT NULL,   -- 'reminder_pending' | 'reminder_sent' | 'reminder_failed' | 'reconciliation_required'
  provider_message_id TEXT,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (invoice_id, reminder_stage)
);

CREATE TABLE invoice_reminder_events (
  idempotency_key TEXT PRIMARY KEY,
  invoice_id      TEXT NOT NULL,
  reminder_stage  TEXT NOT NULL,
  result_status   TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`invoices` is populated by whatever system owns real invoice/payment data — this workflow does not create or update invoice records itself, and never changes an invoice's `status`, `amount`, `recipient_phone`, or `due_date`. `invoice_reminder_state` tracks, per `(invoice_id, reminder_stage)`, whether that specific stage's reminder is in flight, completed, failed, or unresolved. `invoice_reminder_events` binds every `idempotencyKey` to the exact `(invoice_id, reminder_stage)` pair it was first reserved with — this is the request-binding mechanism, exactly as `reply_events.reply_event_id` does for the confirmation/cancellation workflow.

**The reservation function**, modeled directly on `process_reply_event`:

```sql
CREATE OR REPLACE FUNCTION process_invoice_reminder(
  p_idempotency_key   TEXT,
  p_invoice_id        TEXT,
  p_reminder_stage    TEXT,
  p_expected_version  INTEGER
) RETURNS TABLE (
  route                TEXT,   -- 'owner_applied' | 'duplicate_match' | 'idempotency_mismatch'
  result_status         TEXT,
  out_recipient_phone   TEXT,
  out_amount            TEXT,
  out_currency          TEXT,
  out_due_date          TEXT,
  out_new_version        INTEGER
) AS $$
DECLARE
  v_existing invoice_reminder_events%ROWTYPE;
  v_inv invoices%ROWTYPE;
  v_state invoice_reminder_state%ROWTYPE;
BEGIN
  BEGIN
    INSERT INTO invoice_reminder_events (idempotency_key, invoice_id, reminder_stage, result_status)
    VALUES (p_idempotency_key, p_invoice_id, p_reminder_stage, 'reserved');
  EXCEPTION WHEN unique_violation THEN
    SELECT * INTO v_existing FROM invoice_reminder_events WHERE idempotency_key = p_idempotency_key;
    IF v_existing.invoice_id = p_invoice_id AND v_existing.reminder_stage = p_reminder_stage THEN
      RETURN QUERY SELECT 'duplicate_match'::TEXT, v_existing.result_status, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::INTEGER;
    ELSE
      RETURN QUERY SELECT 'idempotency_mismatch'::TEXT, 'idempotency_mismatch'::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::INTEGER;
    END IF;
    RETURN;
  END;

  SELECT * INTO v_inv FROM invoices WHERE invoice_id = p_invoice_id FOR UPDATE;

  IF NOT FOUND THEN
    UPDATE invoice_reminder_events SET result_status = 'missing_invoice' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'missing_invoice'::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::INTEGER;
    RETURN;
  END IF;

  IF v_inv.version <> p_expected_version THEN
    UPDATE invoice_reminder_events SET result_status = 'conflict' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'conflict'::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_inv.version;
    RETURN;
  END IF;

  IF v_inv.status <> 'unpaid' THEN
    UPDATE invoice_reminder_events SET result_status = v_inv.status WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, v_inv.status, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_inv.version;
    RETURN;
  END IF;

  IF v_inv.due_date >= now() THEN
    UPDATE invoice_reminder_events SET result_status = 'not_due' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'not_due'::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_inv.version;
    RETURN;
  END IF;

  SELECT * INTO v_state FROM invoice_reminder_state
    WHERE invoice_id = p_invoice_id AND reminder_stage = p_reminder_stage FOR UPDATE;

  IF FOUND AND v_state.status IN ('reminder_pending', 'reminder_sent') THEN
    UPDATE invoice_reminder_events SET result_status = 'already_reminded' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'already_reminded'::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_inv.version;
    RETURN;
  END IF;

  INSERT INTO invoice_reminder_state (invoice_id, reminder_stage, status, updated_at)
  VALUES (p_invoice_id, p_reminder_stage, 'reminder_pending', now())
  ON CONFLICT (invoice_id, reminder_stage) DO UPDATE SET status = 'reminder_pending', updated_at = now();

  UPDATE invoice_reminder_events SET result_status = 'reminder_pending' WHERE idempotency_key = p_idempotency_key;

  RETURN QUERY SELECT 'owner_applied'::TEXT, 'reminder_pending'::TEXT,
    v_inv.recipient_phone, v_inv.amount, v_inv.currency, v_inv.due_date::TEXT, v_inv.version;
END;
$$ LANGUAGE plpgsql;
```

**Why the reservation `INSERT` (not a same-snapshot check) is the actual safety mechanism:** Postgres's unique index on `idempotency_key` guarantees exactly one concurrent caller can ever complete that `INSERT` — independent of statement snapshot timing. The loser's `EXCEPTION WHEN unique_violation` handler runs only *after* Postgres resolves the conflict against the real, committed winning row (blocking on it if necessary), so the loser's `SELECT` of the existing row is never reading a stale pre-commit snapshot. Verified directly: two concurrent processes racing the same `idempotencyKey` (one held open across the statement via `BEGIN; ...; pg_sleep(...); COMMIT;`, the other executing concurrently) produce exactly one `owner_applied` and one `duplicate_match` reporting the winner's real, correct status — never `NULL`, never a stale value.

**Why only `reminder_pending`/`reminder_sent` block a new attempt, not `reminder_failed`/`reconciliation_required`:** this matches the same pattern `process_reply_event`'s own `calendar_sync_status` guard uses — a genuinely in-flight or already-completed attempt must never be duplicated, but a failed or unresolved one should remain retriable via a deliberately-issued fresh `idempotencyKey` from whatever calls this workflow, not be permanently stuck.

**Finalize (version-guarded, modeled on `Finalize Calendar Result`):**

```sql
CREATE OR REPLACE FUNCTION finalize_invoice_reminder(
  p_idempotency_key   TEXT,
  p_invoice_id        TEXT,
  p_reminder_stage    TEXT,
  p_expected_version  INTEGER,
  p_outcome           TEXT,   -- 'sent' | 'failed'
  p_provider_message_id TEXT
) RETURNS TABLE (out_ok BOOLEAN, out_result_status TEXT) AS $$
DECLARE
  v_new_status TEXT;
BEGIN
  v_new_status := CASE WHEN p_outcome = 'sent' THEN 'reminder_sent' ELSE 'reminder_failed' END;

  UPDATE invoice_reminder_state
  SET status = v_new_status, provider_message_id = p_provider_message_id, updated_at = now()
  WHERE invoice_id = p_invoice_id AND reminder_stage = p_reminder_stage
    AND EXISTS (SELECT 1 FROM invoices WHERE invoice_id = p_invoice_id AND version = p_expected_version);

  IF NOT FOUND THEN
    UPDATE invoice_reminder_state SET status = 'reconciliation_required', updated_at = now()
      WHERE invoice_id = p_invoice_id AND reminder_stage = p_reminder_stage;
    UPDATE invoice_reminder_events SET result_status = 'reconciliation_required' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT false, 'reconciliation_required'::TEXT;
    RETURN;
  END IF;

  UPDATE invoice_reminder_events SET result_status = v_new_status WHERE idempotency_key = p_idempotency_key;
  RETURN QUERY SELECT true, v_new_status;
END;
$$ LANGUAGE plpgsql;
```

`p_expected_version` is the invoice version the *reservation* call observed. If the invoice has moved on by the time the finalize call runs (e.g. paid in the interim, by whatever external system owns payment status) — meaning the send may have gone out for an invoice that's no longer in the state it was reminded about — the guard does not match, `invoice_reminder_state` is left at the honest `reconciliation_required`, and the write is never silently applied against a superseded invoice. Verified directly: bumping the invoice's version between reservation and finalize causes finalize to correctly land on `reconciliation_required`, leaving the prior reservation's `provider_message_id` field untouched (never overwritten with the stale finalize attempt's value).

## Sender sub-workflow binding

`Call Sender` is an Execute Workflow node with `source: "database"` and `workflowId: {"mode": "id", "value": "R1QDUW9jYqxREyDS", "cachedResultName": "WhatsApp Template Message Sender"}` — the exact same reference mechanism [`whatsapp-appointment-reminder`](whatsapp-appointment-reminder.md) already established and documented in detail (see its [Sender sub-workflow binding design](whatsapp-appointment-reminder.md#sender-sub-workflow-binding-design)): the sender's committed JSON ships with that fixed top-level `id` baked in, `n8n import:workflow` preserves a committed `id` unchanged, and a conflicting-id import fails with an explicit error rather than silently misbinding. Re-verified directly for this workflow (not merely assumed from the prior workflow's findings): imported the real, unmodified sender alongside this workflow into a fresh instance and confirmed the reference resolves without manual rebinding, and re-confirmed the same on a second, independently clean instance after a full CLI export/import round trip. See [Clean re-import results](#clean-re-import-results).

This workflow supplies the sender's six inputs itself: `recipientPhone` and the body-parameter values come from Postgres (via the reservation call); `templateName`/`languageCode` come from the hardcoded `STAGE_TEMPLATES` map in `Build Send Request`, keyed by the caller's (strictly allowlisted) `reminderStage`; `graphApiVersion`/`phoneNumberId` are hardcoded constants in the same node. **If you re-export or otherwise change the sender workflow in a way that changes its `id`,** this reference will break explicitly (Execute Workflow surfaces a clear error, it does not fail open) — update `Call Sender`'s `workflowId.value` to match.

## Crash and reconciliation behavior

The complete state machine this workflow can leave an invoice/reminder-stage pair in:

| State | Meaning |
|---|---|
| *(no `invoice_reminder_events` row)* | This `idempotencyKey` was never durably reserved — either rejected before the atomic write, or the atomic write never ran (e.g. Postgres was unavailable). |
| `missing_invoice` | The atomic write ran; no invoice with this id exists. |
| `conflict` | The atomic write ran; `expectedVersion` did not match the invoice's current version. |
| `paid` / `cancelled` / `disputed` | The atomic write ran; the invoice's real, current status makes a reminder inappropriate. |
| `not_due` | The atomic write ran; the invoice's due date has not yet passed (checked against Postgres's own clock). |
| `already_reminded` | The atomic write ran; this `(invoiceId, reminderStage)` pair already has a reminder in flight or already sent. |
| `reminder_pending` | The atomic write committed this durable pending marker before any WhatsApp call was attempted. This is the only state a crash between the database write and the send call (or between the send call and finalizing its result) can leave visible. |
| `reminder_sent` | The send succeeded and was finalized via the version-guarded write — full success. |
| `reminder_failed` | The send was attempted and failed (any non-2xx status or a transport failure), finalized via the version-guarded write. Retriable via a fresh `idempotencyKey`. |
| `reconciliation_required` | The version-guarded finalize write's guard did not match — the invoice moved on since the reservation; `invoice_reminder_state` was left untouched by the (possibly stale) finalize attempt; a human or a separate reconciliation process must resolve this by hand. |

**If the process crashes after the atomic database write commits but before or during the send call**, the database durably and visibly shows `reminder_pending`. A duplicate request for the same `idempotencyKey` reports this exact pending state and makes zero new sends (the `invoice_reminder_events` reservation already exists). A fresh `idempotencyKey` for the same `(invoiceId, reminderStage)` is rejected as `already_reminded`, also making zero sends — this workflow never allows a second overlapping attempt at the same stage regardless of which idempotency key is used.

**If the send itself succeeds or fails but the final Postgres write then fails** (e.g. Postgres becomes unreachable at exactly that moment), this workflow does **not** automatically retry the send — the message may already have reached WhatsApp, and retrying blindly risks a duplicate. The execution fails loudly (no `continueOnFail` on the finalize node), and the reminder state is left showing `reminder_pending` — its state from the initial atomic write — until a human or a separate reconciliation process checks WhatsApp's actual delivery state and updates Postgres by hand. Verified directly by isolating the finalize write against a deliberately unreachable Postgres connection after a successful mock send: exactly one send occurred, and the state remained durably `reminder_pending` — never falsely marked `reminder_sent`, and a subsequent attempt at the same stage is correctly blocked as `already_reminded` rather than sending a second message.

**Resolving a stuck `reminder_pending` or `reconciliation_required` row is outside this workflow's scope.** It requires checking the actual delivery state on WhatsApp's side and manually updating `invoice_reminder_state` (and, if appropriate, `invoice_reminder_events.result_status`) — this workflow deliberately does not attempt that automatically.

## Required credentials

**One**, not included in the exported JSON — no node has a credential bound after import, by design:

| Credential | Bound to node(s) | Type |
|---|---|---|
| e.g. "Invoices Postgres" | `Reserve And Apply`, `Finalize Send Result` | n8n **Postgres** credential (`postgres`), pointed at your own database with the schema and functions in [Atomic idempotency design](#atomic-idempotency-design) |

The sender's own credential (its HTTP Header Auth WhatsApp access token) lives only in the already-committed [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) workflow — this workflow never touches it directly.

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Setup steps

1. Create the schema — see [Atomic idempotency design](#atomic-idempotency-design) for a new installation.
2. Import `unpaid-invoice-reminder.json`.
3. Create and bind your Postgres credential (see [Required credentials](#required-credentials)) to `Reserve And Apply` and `Finalize Send Result`.
4. Ensure [`whatsapp-template-message-sender.json`](whatsapp-template-message-sender.json) is already imported (with its own WhatsApp credential bound) — if you imported it via the official CLI or the editor's normal import feature without changing its id, `Call Sender`'s reference resolves automatically. If you ever re-export the sender in a way that changes its id, update `Call Sender`'s `workflowId.value` to match.
5. **Edit `Build Send Request`'s hardcoded `PHONE_NUMBER_ID` constant to your real WhatsApp Business phone number id**, and `STAGE_TEMPLATES` to your own real, Meta-approved template names — this workflow ships with placeholder values that will not send successfully against the real API. `GRAPH_API_VERSION` should be reviewed against Meta's currently-supported version.
6. Populate `invoices` from your real billing data however you already do that — this workflow does not create or update invoice records itself.
7. Build whatever calls this sub-workflow — a scheduled job or finance-team action that decides *which* invoice/stage combinations are worth attempting right now, and calls this workflow once per candidate, passing a fresh `idempotencyKey` per attempt, the `invoiceId`, the decided `reminderStage`, and `expectedVersion` looked up from your own invoice records. **Do not pass a recipient phone, amount, currency, or due date** — none of these are part of this workflow's input contract. **Do not reuse an `idempotencyKey` for a genuinely different reminder attempt** — see [Atomic idempotency design](#atomic-idempotency-design) for what happens if you do (an explicit `idempotency_mismatch`, not silent corruption, but also not processed).
8. Have a plan for resolving `reminder_pending`/`reconciliation_required` rows that don't clear on their own — see [Crash and reconciliation behavior](#crash-and-reconciliation-behavior). This workflow does not do this automatically.
9. Test with synthetic data against your own isolated setup first.

## Test procedure

Built and verified in an isolated local n8n test environment (the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2 via `nvm`, isolated `N8N_USER_FOLDER`) against isolated local **PostgreSQL 16.15** instances (fresh `initdb` clusters, non-default ports, short unix-socket paths, synthetic data only — long scratch paths break Postgres sockets). **The real Meta/WhatsApp API was never contacted** — every test requiring a send used a temporary, uncommitted mock-bound copy of both this workflow and the sender, targeting a local mock HTTP server instead of `https://graph.facebook.com`, never exported or committed. Confirmed by inspecting the mock server's own request log after every test run: every request target was `127.0.0.1`, never `graph.facebook.com`.

**CLI-only test methodology.** `n8n execute --id` does not accept custom trigger input directly, so each scenario used a small, throwaway, never-committed "test caller" workflow (Manual Trigger → Code node with the scenario's fixed input → Execute Workflow node calling the real target by id with auto-mapped input), imported and executed via the official CLI, with the target workflow's controlled output read back from the caller's own execution result. Two CLI-specific findings worth recording: (1) n8n 2.35.4 requires a workflow to be `n8n publish:workflow`ed, not just imported, before another workflow can call it via Execute Workflow — otherwise "Workflow is not active and cannot be executed"; (2) genuine concurrent execution needs `n8n execute-batch --ids=A,B --concurrency=2` in one process — two separate `n8n execute` processes each start their own internal Task Broker on a fixed port and collide rather than testing anything.

| # | Test | Result | Verified via |
|---|---|---|---|
| 1 | Valid overdue unpaid invoice | Exactly one mock send; `reminder_sent` with a real provider message id | mock-bound copy, real n8n execution |
| 2 | Invoice not yet due | `not_due`, zero sends | real committed file, real n8n execution |
| 3 | Paid invoice | `paid`, zero sends | real committed file, real n8n execution |
| 4 | Cancelled invoice | `cancelled`, zero sends | real committed file, real n8n execution |
| 5 | Disputed invoice | `disputed`, zero sends | real committed file, real n8n execution |
| 6 | Missing invoice | `missing_invoice`, zero sends | real committed file, real n8n execution |
| 7 | Invalid (path-traversal-shaped) `invoiceId` | `rejected`, `invoiceId` nulled out (never echoes an unvalidated value), zero Postgres mutation, zero sends | real committed file, real n8n execution |
| 8 | Stale `expectedVersion` | `conflict`, zero sends | real committed file, real n8n execution, direct SQL |
| 9 | Sequential duplicate (same `idempotencyKey` replayed) | Zero additional sends; the real, prior stored status is returned, never a generic label — confirmed both for a still-`reminder_pending` original and a `reminder_failed` original | real n8n execution |
| 10 | Concurrent identical duplicate (same `idempotencyKey`, genuinely racing) | Exactly one mock send (call-count delta = 1 across both executions); the winner reports `reminder_sent`, the loser reports the real, live `reminder_pending`/`reminder_sent` status via `duplicate_match` — never `NULL`, never stale | direct SQL (`BEGIN`/`pg_sleep`/concurrent-session technique), `n8n execute-batch --concurrency=2` against the real mock-bound graph |
| 11 | Same `idempotencyKey`, different `invoiceId` | `idempotency_mismatch`, zero mutation/send for the loser | real n8n execution |
| 12 | Same `idempotencyKey`, different `reminderStage` | `idempotency_mismatch` | real n8n execution |
| 13 | Postgres unavailable before reservation | Postgres stopped entirely: execution fails loudly (`Connection refused`) at `Reserve And Apply`; zero rows written; mock server call count unchanged | direct isolation test, real n8n execution |
| 14 | Crash immediately after reservation | Durable, explicit `reminder_pending` — confirmed directly in Postgres | direct SQL inspection |
| 15 | Sender `400`/`401`/`404`/`429`/`500`/timeout | Each on its own fresh invoice+stage: `reminder_failed` with the correct `httpStatus` (`null` for the transport-level timeout), `invoice_reminder_state` lands on `reminder_failed` in every case, no retry | mock-bound copy, real n8n execution |
| 16 | Postgres unavailable after sender success | `Finalize Send Result` bound to a deliberately unreachable Postgres connection after a successful mock send: exactly one send occurred (`wamid` issued), the finalize write fails loudly, state remains durably `reminder_pending` — never falsely marked `reminder_sent`. Confirmed no automatic retry (state unchanged on a later plain check), and a subsequent attempt at the same stage with a fresh `idempotencyKey` is correctly blocked as `already_reminded` with zero additional mock calls | mock-bound copy, targeted Postgres-connection isolation, real n8n execution |
| 17 | Finalize against a superseded invoice version | Using a deliberately slow mock response to create a real window: the invoice's `version` was bumped (simulating an external payment update) while a send was still in flight. Result: the send genuinely completed (`httpStatus: 200`, a real provider message id was issued) but the workflow's own top-level result is `reconciliation_required`, and `invoice_reminder_state.provider_message_id` was never overwritten with that stale value | direct SQL inspection, real n8n execution against the actual graph |
| 18 | Malformed sender output | The real sender is well-behaved by design (verified: a 2xx response missing the expected `messages` field is classified `sent` with a `null` provider message id, per the sender's own already-established, already-verified contract — not a bug in this workflow). Genuinely malformed shapes (`{}`, non-string `status`, non-numeric `httpStatus`, non-string `providerMessageId`) were verified by direct inspection of `Classify Send Result`'s code: never crashes, only ever reports `outcome: 'sent'` when `resp.status === 'sent'` exactly, always nulls out a wrong-typed `httpStatus`/`providerMessageId` rather than propagating it | real n8n execution (2xx-missing-field case), direct code inspection (all other malformed shapes) |
| 19 | Controlled output contains no sensitive fields | Every seeded test invoice's `recipient_phone` and `amount` value, checked against the final controlled output of every test execution: zero matches | real n8n execution |
| 20 | Execution-data persistence settings verified | Same settings block already verified for [`whatsapp-appointment-confirmation-cancellation`](whatsapp-appointment-confirmation-cancellation.md) and [`whatsapp-appointment-reminder`](whatsapp-appointment-reminder.md) (`saveDataErrorExecution`/`saveDataSuccessExecution: none`, `saveManualExecutions`/`saveExecutionProgress: false`). Under this CLI-only (not server+REST-API) test methodology, a canary-keyed execution's underlying sub-execution row was present in SQLite but permanently stuck unfinished (`status: "running"`, `finished: 0`) — never normally completable or retrievable through ordinary means — consistent with those workflows' own documented finding that this settings block prevents normal retrieval of a completed execution's data, even though the exact storage-layer signature differs by test methodology | real n8n execution, direct SQLite inspection |
| 21 | Official CLI export/import | `n8n export:workflow --id=<id> --output=<dir>/ --separate --pretty` (the plain `--output=file.json` form produces an array-wrapped format, not the single-workflow shape this repository's files use) into a clean instance, re-exported from a **second**, fully separate clean instance with a freshly-created Postgres database and the schema reinstalled: `nodes`, `connections`, `settings` byte-for-byte identical at every hop (content-verified field-by-field, not just eyeballed). The sender-by-id reference resolved on the second instance with zero manual rebinding, confirmed both structurally (`not_due` path) and behaviorally (mock-bound send path: `reminder_sent`, a real provider message id, exactly one mock call) | real committed file, second fully independent clean instance |

The committed JSON was produced by this exact CLI export, then had n8n's own instance-specific metadata fields (`active`, `createdAt`, `updatedAt`, `versionId`, `triggerCount`, and similar) stripped to match every other workflow package in this repository's identical committed shape (`connections`, `id`, `meta`, `name`, `nodes`, `pinData`, `settings`, `staticData`, `tags`) — confirmed field-by-field that this stripping changed nothing about the nodes, connections, or settings themselves.

All test data was synthetic: fake invoice/phone/idempotency-key identifiers, a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, and a local mock server — no real Postgres database, WhatsApp/Meta credentials, or customer data anywhere.

## Known limitations

- **Does not decide reminder cadence or policy.** Which invoices are overdue enough to warrant a `stage1` vs. `stage2` vs. `final` attempt, and when, is entirely outside this workflow's scope — it only verifies eligibility for and applies a single already-decided attempt.
- **Does not create, update, or seed invoice records.** `invoices` must already be populated by whatever system owns real billing data.
- **A stuck `reminder_pending` or `reconciliation_required` row requires manual or separate-workflow resolution.** This workflow deliberately never automatically retries a send for a pending or unresolved stage, because a prior request may already have reached WhatsApp.
- **A compromised Postgres or WhatsApp sender credential defeats this workflow's own guarantees entirely** — the optimistic-concurrency and idempotency mechanisms protect against races and duplicate processing, not against a credential that shouldn't have been trusted in the first place.
- **No automatic retries, intentionally, anywhere.**
- **Execution-data persistence is disabled by this workflow's settings, but physical deletion is asynchronous and bounded, not instantaneous** — consistent with the same finding already documented for the sibling workflows using these settings.
- **Only three reminder stages are supported** (`stage1`/`stage2`/`final`) — any other value is rejected, not silently accepted.
- **The real Meta/WhatsApp API was never contacted during testing** — send behavior was verified only through a temporary, uncommitted mock-bound copy, disclosed precisely above. Verify against your own real, non-production WhatsApp Business setup before relying on this.
- This workflow has been verified as a template against the specific n8n, Node.js, and PostgreSQL versions documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads `idempotencyKey`, `invoiceId`, `reminderStage`, and `expectedVersion` from its caller — **not** a recipient phone, amount, currency, or due date, all of which are read exclusively from Postgres. Reads and writes `invoices` (read-only — this workflow never changes an invoice's own status/amount/recipient/due date), `invoice_reminder_state`, and `invoice_reminder_events`. Makes at most one outbound call — to the existing sender sub-workflow, which itself makes at most one outbound HTTP call to the fixed WhatsApp Business Cloud API host — only after Postgres durably records `reminder_pending`. Its controlled output contains only `status`, `invoiceId`, `reminderStage`, `httpStatus`, and `providerMessageId` — never the recipient phone, amount, currency, template content, database details, credentials, or raw provider responses.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-25
