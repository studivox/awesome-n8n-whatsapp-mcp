# WhatsApp Appointment Reminder

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook, not a scheduler) that decides whether a single appointment's reminder is due right now, and — only when it genuinely is — calls the existing [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) sub-workflow to actually send it.

It accepts an appointment's `appointmentId`, `recipient`, `appointmentStart` (an ISO-8601 timestamp with an **explicit** UTC offset or `Z`), `reminderLeadMinutes`, `sendWindowMinutes`, and the sender's own `graphApiVersion`/`phoneNumberId`/`templateName`/`languageCode`/optional `bodyParameters`. It computes `dispatchAt = appointmentStart - reminderLeadMinutes`, compares it against the real execution-time clock, and classifies the request as `not_due`, `due`, `expired`, or `rejected` — see [Reminder timing rules](#reminder-timing-rules). Only a `due` classification results in a sub-workflow call; every other outcome makes **zero** HTTP requests, structurally guaranteed by an IF node gating the Execute Workflow node (the same pattern the sender itself uses to gate its own HTTP Request node).

It does **not** send a WhatsApp message itself, does **not** build the Meta API request, and does **not** know anything about HTTP, template bodies, or the Graph API host — all of that lives only in the sender sub-workflow, which this workflow calls by reference rather than duplicating.

## Real business use case

Reducing no-shows requires sending a reminder message at the right time relative to an appointment — not immediately, and not after it's too late to matter. This workflow is the "is it time yet, and if so send it" decision step: something else (a Schedule Trigger polling upcoming appointments, a queue consumer, a webhook from a booking system) is expected to call this once per candidate appointment; this workflow decides whether *this particular call, right now* should actually result in a message, and if so, delegates the send itself to the already-tested sender sub-workflow.

**This is explicitly not a scheduler and not a full reminder platform.** It does not read a calendar, does not query a database, does not know what appointments exist, and does not track which appointments have already been reminded — see [Known limitations](#known-limitations).

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22) — the same versions used for every other workflow in this repository.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the ten-field input contract.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope/limitation notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used three times: input validation + timing classification ("Validate & Classify"), and two controlled-response builders ("Build Due Response", "Build Non-Sender Response").
- **IF** (`n8n-nodes-base.if`, v2.3) — "Due Now?"; the single gate between validated input and the sender call. This is the structural guarantee behind "zero HTTP requests for rejected/not_due/expired" — the Execute Workflow node sits only on its `true` output.
- **Execute Workflow** (`n8n-nodes-base.executeWorkflow`, v1.2) — "Call Sender"; the only node in this workflow that can trigger network activity, and only reachable when `_route === 'due'`. See [Sender sub-workflow binding design](#sender-sub-workflow-binding-design).

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature.

## Sender sub-workflow binding design

This was the central feasibility question for this workflow, and it was answered experimentally, not assumed.

**How Execute Workflow actually resolves a sub-workflow.** The node's `source: "database"` mode stores a `workflowId` object of the form `{ "mode": "id", "value": "<workflow id>" }`. Confirmed by direct inspection of a live n8n v2.35.4 instance: creating a workflow with a given `id` via the standard workflow-creation API path (the same path both the CLI's `import:workflow` and the editor's own save/import use) **preserves that exact id** — it is not silently regenerated. A second workflow's Execute Workflow node referencing that id by `mode: "id"` correctly resolves and calls it, confirmed by an actual end-to-end execution.

**Why this makes a portable reference safe here, specifically.** The committed [`whatsapp-template-message-sender.json`](whatsapp-template-message-sender.json) has always shipped with a fixed top-level `id` (`R1QDUW9jYqxREyDS`) baked in from its original export — this is not a live customer's private data or a per-installation secret, it is simply that file's own stable, already-public identifier, sitting in this same repository. Anyone who imports the sender via the documented method (official CLI, or the standard import path the n8n editor itself uses) gets that exact id back. This workflow's `Call Sender` node references it directly: `{"mode": "id", "value": "R1QDUW9jYqxREyDS"}`.

**What was verified, not assumed:**
- Confirmed experimentally that `n8n import:workflow` (official CLI) preserves a workflow's committed `id` unchanged.
- Confirmed experimentally that the same is true of the underlying workflow-creation API path used by both the CLI and the editor's own import feature.
- Confirmed experimentally, on a **second, genuinely clean** n8n instance (fresh empty data directory), that importing the sender and this reminder workflow independently, then activating both, results in a correctly-resolved reference — no manual rebinding of the sub-workflow pointer was needed.
- Confirmed experimentally that if a workflow with a conflicting id already exists, workflow creation fails with an explicit `400` error ("Workflow with id X exists already") — it does not silently bind to the wrong workflow.
- Confirmed experimentally that **n8n requires a referenced sub-workflow to be activated/published before the calling workflow can itself be activated** (relevant only if whatever calls this reminder sub-workflow needs to be active, e.g. because it is driven by a Schedule Trigger — the reminder workflow and the sender it references would both need to be activated too in that case; Execute Workflow can call an *inactive* sub-workflow directly by id without issue, confirmed separately).

**If you re-export or otherwise change the sender workflow in a way that changes its `id`,** this reference will break (pointing at a nonexistent id) rather than silently misfire — Execute Workflow surfaces a clear error in that case, it does not fail open. Update the `workflowId.value` in the `Call Sender` node to match if you ever do this.

**No fabricated or placeholder id was used.** This design was chosen specifically *because* the reference could be verified as portable and correct — the fallback path (ship the node unbound, fail closed, document manual binding) was not needed and was not used.

## Reminder timing rules

```
dispatchAt = appointmentStart - reminderLeadMinutes
windowEnd  = dispatchAt + sendWindowMinutes
```

Classification, using the real execution-time clock (`Date.now()` at the moment "Validate & Classify" runs — there is **no input field** a caller can use to override this; this workflow always uses its own actual system clock):

| Condition | Result |
|---|---|
| `now < dispatchAt` | `not_due` — zero sender calls |
| `dispatchAt <= now <= windowEnd` | `due` — sender called exactly once |
| `now > windowEnd` | `expired` — zero sender calls |
| Any input fails validation (see below), regardless of timing | `rejected` — zero sender calls |

**The window is closed (inclusive) on both ends, by design.** `now === dispatchAt` and `now === windowEnd` both count as `due`. This is the deterministic tie-break rule for the exact-boundary case — verified directly by test.

**Validation runs unconditionally, before any timing check.** An appointment with, say, an invalid `templateName` is always `rejected`, never `not_due` or `expired`, regardless of its timestamp — timing is only evaluated once every field has already passed validation. Validated fields and their bounds:

| Field | Rule |
|---|---|
| `appointmentId` | `^[A-Za-z0-9_-]{1,128}$` — an opaque bounded identifier, not interpreted |
| `recipient` | `^[1-9]\d{7,14}$` — identical shape to the sender's own `recipientPhone` validation |
| `appointmentStart` | ISO-8601 with an explicit `Z` or `±HH:MM` offset; a bare local timestamp is rejected — this workflow never guesses a customer's timezone |
| `reminderLeadMinutes` | integer, 1–10080 (7 days) |
| `sendWindowMinutes` | integer, 1–1440 (24 hours) |
| `graphApiVersion`, `phoneNumberId`, `templateName`, `languageCode`, `bodyParameters` | identical rules to the sender's own validation (see [its documentation](whatsapp-template-message-sender.md#security-design)) |

The sender-bound fields are validated **here too**, before the sub-workflow is ever called — this is deliberate pre-validation duplication (so an input the sender would reject is rejected here, before any sub-workflow invocation), not a reimplementation of the sender's HTTP request or template-body construction logic, which exists only in the sender itself.

**An appointment already in the past** is handled by the same arithmetic, with no special-cased branch: if `dispatchAt` and `windowEnd` are both already behind `now`, the result is `expired` — the same deterministic classification as any other appointment whose window has closed.

## Required credentials

**None, directly.** This workflow makes no HTTP requests and holds no secrets itself — the sender sub-workflow it calls has its own credential requirement (an HTTP Header Auth credential bound to its `Send Template Message` node); see [its documentation](whatsapp-template-message-sender.md#required-credentials). You must set that up on the sender exactly as documented there; this workflow does not need any credential of its own.

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Setup steps

1. Import [`whatsapp-template-message-sender.json`](whatsapp-template-message-sender.json) first, if you haven't already, and complete its own setup steps (create and bind its Header Auth credential).
2. Import `whatsapp-appointment-reminder.json`. Its `Call Sender` node references the sender by the sender's committed id (`R1QDUW9jYqxREyDS`) — if you imported the sender via the official CLI or the editor's normal import feature without changing its id, this reference resolves automatically; no manual rebinding is required. If you ever re-export the sender in a way that changes its id, update `Call Sender`'s `workflowId.value` to match (see [Sender sub-workflow binding design](#sender-sub-workflow-binding-design)).
3. Build whatever calls this sub-workflow — a Schedule Trigger that polls your own appointment source and calls this workflow once per candidate appointment via an Execute Workflow node, passing the ten input fields. This repository does not provide that caller: see [Known limitations](#known-limitations).
4. If the workflow that calls this one needs to be **activated** (e.g. it's driven by a Schedule Trigger), n8n requires this reminder workflow, and the sender it references, to also be activated first — see [Sender sub-workflow binding design](#sender-sub-workflow-binding-design).
5. **Read [No idempotency protection](#known-limitations) before connecting this to anything that calls it more than once per appointment.**
6. Test with synthetic data first — see [Test procedure](#test-procedure).

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched). **The real Meta API was never contacted for any due-path/HTTP-behavior test.**

Testing was split, for the same reason the sender's own testing is split (see [its test procedure](whatsapp-template-message-sender.md#test-procedure)):

**1. Timing and validation logic — tested end-to-end against the actual shipped workflow, referencing the real sender by id:** every `not_due`, `expired`, and `rejected` scenario is, by construction, blocked by the `Due Now?` IF node before the `Call Sender` node ever runs — so these were safe to execute against the real, unmodified committed files. Confirmed via each execution's recorded node history that `Call Sender` never ran for any of these cases, and confirmed (via the sender's own committed, unmodified `Send Template Message` node) that no HTTP request could have been made regardless, since the IF node structurally prevents `Call Sender` from executing at all.

**2. Due-path / HTTP-status behavior — tested only through a temporary, uncommitted mock-bound copy:** a temporary duplicate of the sender (identical except its `Validate & Build Request` node targeted a local mock HTTP server, `http://127.0.0.1:8765`, Python standard library, not part of this repository, instead of the fixed production host) and a temporary duplicate of this reminder workflow (identical except its `Call Sender` node referenced that mock-bound sender copy instead of the real one, and its Execute Workflow Trigger accepted one additional test-only `mockStatus` field used only to steer the mock server's response). Neither temporary copy was ever exported or committed. Every scenario below that requires the sub-workflow to actually be called was run only through this mock-bound pair — never against the real, fixed-host sender.

| # | Test | Result | Verified via |
|---|---|---|---|
| 1 | Not due yet | `not_due`, zero sender calls | real committed files |
| 2 | Exact dispatch boundary | `due`, sender called once | mock-bound pair |
| 3 | Inside send window | `due`, sender called once | mock-bound pair |
| 4 | Near/at window-end boundary | Deterministic per the documented inclusive rule | mock-bound pair |
| 5 | Window expired | `expired`, zero sender calls | real committed files |
| 6 | Appointment already in the past | `expired`, zero sender calls | real committed files |
| 7 | Missing `appointmentId` | `rejected`, zero sender calls | real committed files |
| 8 | Invalid `recipient` | `rejected`, zero sender calls | real committed files |
| 9 | `appointmentStart` without a timezone offset | `rejected`, zero sender calls | real committed files |
| 10 | Invalid `appointmentStart` | `rejected`, zero sender calls | real committed files |
| 11 | Invalid `reminderLeadMinutes` (e.g. negative) | `rejected`, zero sender calls | real committed files |
| 12 | Invalid `sendWindowMinutes` (e.g. zero) | `rejected`, zero sender calls | real committed files |
| 13 | Invalid sender-bound input (e.g. malformed `templateName`) despite being otherwise due | `rejected` before any sub-workflow call, zero sender calls | real committed files |
| 14 | Valid reminder with `bodyParameters` | `sent`, parameters correctly forwarded to the sender | mock-bound pair |
| 15 | Sender success | `sent`, real `providerMessageId` returned | mock-bound pair |
| 16 | Sender HTTP 400 | `provider_rejected`, `httpStatus: 400` | mock-bound pair |
| 17 | Sender HTTP 401 | `auth_error`, `httpStatus: 401` | mock-bound pair |
| 18 | Sender HTTP 429 | `rate_limited`, `httpStatus: 429` | mock-bound pair |
| 19 | Sender HTTP 500 | `provider_error`, `httpStatus: 500` | mock-bound pair |
| 20 | Sender timeout | `timeout`, `httpStatus: null` | mock-bound pair |
| 21 | Failure causes exactly one invocation, no retry | Confirmed via the mock server's own request log: exactly one request recorded per failing scenario | mock-bound pair |
| 22 | Same due input invoked twice | **Two sends resulted** — confirmed the documented lack of idempotency is real, not just a theoretical caveat; see [Known limitations](#known-limitations) | mock-bound pair |
| 23 | Execution history/API/SQLite persistence behavior | See [Persistence verification](#persistence-verification) below | real committed files (reminder side); mock-bound pair (sender side, standing in for the real sender's own already-verified settings) |
| 24 | Clean export/import preserves settings and logic | Exported the real committed workflow via the official CLI, imported into a clean instance, exported again — `settings`, `nodes`, and `connections` were byte-for-byte identical | real committed files |
| 25 | Second clean instance produces identical results | A genuinely separate, freshly-initialized n8n instance (fresh empty data directory) reproduced every applicable result above identically, including confirming the sender-by-id reference resolved correctly without manual rebinding | real committed files (timing/validation cases) + a fresh mock-bound pair built on that same clean instance (due-path cases) |

An earlier, exploratory feasibility check (confirming that an Execute Workflow node with `mode: "id"` can call a sub-workflow at all) was run once against the real, unmodified sender with synthetic/fake credentials and a fake phone number before this test methodology was finalized. That check reached the real `https://graph.facebook.com` host (confirmed: this environment has outbound internet access) and received a transport-level failure back — no valid access token, template, or real phone number was ever used, so no message could have been sent, but this did constitute one unintended contact with Meta's real endpoint. All due-path testing from that point forward used only the mock-bound methodology described above, and the shipped workflow's `Call Sender` node was never executed against the real sender with real network reachability in any test counted above.

All test data was synthetic: fake phone numbers (`100000000xxx`), a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, synthetic `CANARY_...`-labeled appointment identifiers for persistence testing, and a local mock server — no real Meta credentials, phone numbers, templates, or customer data anywhere. A repository-wide scan for every canary/test value used in this testing found none committed to any file.

## Persistence verification

This workflow's `settings` disable n8n's execution-data retention, using the same settings verified in the WhatsApp Webhook Security Gateway's correction (see [its "Execution-data persistence" section](whatsapp-webhook-security-gateway.md#execution-data-persistence) for the full mechanism):

```json
{
  "executionOrder": "v1",
  "saveDataErrorExecution": "none",
  "saveDataSuccessExecution": "none",
  "saveManualExecutions": false,
  "saveExecutionProgress": false
}
```

Verified experimentally, with a unique canary `appointmentId`, against both this reminder workflow and the mock-bound sender copy standing in for the real sender's own execution:

- Immediately after a `due` execution, both the reminder's own execution and the sender sub-workflow's execution were **invisible** via n8n's single-execution detail API (`GET /rest/executions/:id` returned `{}` for both) — confirmed with these settings applied and the workflows reactivated after the settings change (an already-active workflow does not always pick up a settings change until reactivated — see the same finding in the gateway's documentation).
- Direct SQLite inspection (after a WAL checkpoint) found the canary `appointmentId` **still physically present** in `execution_data` for both executions immediately afterward — confirming, as with the gateway, that these settings achieve immediate unreachability via any n8n interface, not instantaneous physical erasure.
- Restarted the n8n process, and confirmed via SQLite that the canary rows were still present immediately post-restart (soft-deleted, pending n8n's own background pruning), then confirmed physical removal completed under an accelerated pruning-interval test configuration — the canary was absent from the SQL-queryable data and, after a further WAL checkpoint/truncate, from a full raw-byte scan of the database file, for both the reminder's own execution and the sender sub-workflow's execution. Under n8n's **default** pruning configuration this physical removal is bounded but not instantaneous (up to roughly the sum of the default hard-delete interval and buffer) — see the gateway documentation's full explanation; this is standard, unmodified n8n behavior, not something specific to this workflow.
- **Do not read this as "the appointment data never touches disk."** It means: unreachable via any n8n API/UI immediately, and queued for physical deletion by n8n's own standard background process on a bounded schedule.

## Known limitations

- **No idempotency protection, at all.** This workflow is entirely stateless — it holds no record of which appointments have already received a reminder. Invoking it twice with the same due appointment **sends the reminder twice**; this was directly demonstrated, not just asserted (see test #22). If your caller might invoke this more than once for the same appointment (retries, overlapping schedule runs, at-least-once delivery from whatever queues the calls), you **must** implement your own deduplication before calling this workflow — e.g. tracking `appointmentId` values you've already dispatched a reminder for.
- **Not a scheduler.** This workflow has no trigger of its own beyond Execute Workflow Trigger. Something else must decide *which* appointments to check and *when* to call this workflow — it does not poll, does not read a calendar, and does not query a database.
- **No calendar or database integration of any kind.** All appointment data arrives as input parameters from the caller; nothing is read from or written to any external system by this workflow.
- **No delivery-status tracking** — inherited directly from the sender: a `status: "sent"` result means the Graph API *accepted* the request, not that the message was delivered or read.
- **No automatic retries, intentionally** — inherited from the sender's own no-retry design, for the same reason: to avoid silently duplicating sends.
- **A compromised sender credential, or a caller that invokes this workflow more than intended, defeats any of this workflow's own timing logic** — the timing/validation gate only controls *whether* a call reaches the sender, not what the sender or its credential can do once reached.
- **Execution-data persistence is disabled by this workflow's settings, but physical deletion is asynchronous and bounded, not instantaneous** — see [Persistence verification](#persistence-verification).
- **The sender sub-workflow reference is an id, resolved at call time** — see [Sender sub-workflow binding design](#sender-sub-workflow-binding-design) for exactly what was verified about its portability and what happens if it ever breaks.
- This workflow has been verified as a template against the specific n8n version and test suite documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads `appointmentId`, `recipient`, `appointmentStart`, `reminderLeadMinutes`, `sendWindowMinutes`, `graphApiVersion`, `phoneNumberId`, `templateName`, `languageCode`, and optional `bodyParameters` from its caller, only in memory during a single execution. Its own controlled output contains only `status`, `appointmentId`, `dispatchAt`, `httpStatus`, and `providerMessageId` — never the recipient, template content, credentials, request/response bodies, headers, or raw provider errors (the last three are already excluded by the sender's own response contract; this workflow adds nothing beyond it). `appointmentId` and `providerMessageId` are opaque identifiers that may be linkable to a real customer or communication record — this workflow does not claim they are not personal data, and you should treat them with the same care as any other identifier tied to a real person in your own systems. See [Persistence verification](#persistence-verification) for what n8n itself may retain, briefly, regardless of this workflow's own output.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
