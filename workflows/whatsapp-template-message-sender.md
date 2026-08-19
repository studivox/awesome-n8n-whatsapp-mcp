# WhatsApp Template Message Sender

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook) that sends an approved WhatsApp Business Cloud API template message. It accepts `recipientPhone`, `templateName`, `languageCode`, `graphApiVersion`, `phoneNumberId`, and an optional `bodyParameters` array; validates all of them; builds the official WhatsApp template-message request body and destination URL; and, only when valid, makes a single outbound HTTP POST to the **fixed, official Meta Graph API host** (`https://graph.facebook.com`). It returns only non-sensitive delivery metadata: `status`, `httpStatus`, `providerMessageId`.

**This workflow sends a real WhatsApp message when connected to live credentials.** Repository testing used a local mock HTTP server standing in for Meta — the real Meta API was never contacted during development or testing of this package.

**Not production-ready.** It provides no delivery-status tracking (it only reports whether the send *request* was accepted, not whether the message was actually delivered or read), and intentionally has no automatic retries.

**Works unmodified on n8n Cloud and default Community Edition installations** — it requires no instance-level environment variable or security-setting change of any kind (see [Configuration design](#configuration-design)).

## Real business use case

Sending appointment reminders, order updates, or invoice notifications via WhatsApp requires using a pre-approved message *template* (Meta does not allow free-form outbound business-initiated messages). This sub-workflow centralizes that "send an approved template" step — with input validation and a safe, controlled response — so other workflows (e.g. an appointment-reminder scheduler) can call it via n8n's Execute Workflow node instead of duplicating HTTP Request/auth logic.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the six-field input contract.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope/limitation notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used four times: input validation + request/URL building, and three distinct controlled-response builders (success, transport failure, rejected).
- **IF** (`n8n-nodes-base.if`, v2.3) — branches on whether input passed validation, before any HTTP request is made.
- **HTTP Request** (`n8n-nodes-base.httpRequest`, v4.5) — the actual outbound call to the Graph API, configured with `Never Error` + `Full Response` (so both success and non-2xx responses are classified by this workflow, not thrown as node errors) and `onError: continueErrorOutput` (so genuine transport/timeout failures route to a separate controlled branch instead of crashing the execution).

All node types are part of n8n core — no community nodes required.

## Configuration design

**No environment variables, no `$env`/`$vars` expressions, and no instance-level security setting is required or used anywhere in this workflow.** An earlier version of this package used `$env` to read a configurable Graph API base URL, which required setting `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` on the whole n8n instance — a global setting unavailable on n8n Cloud and unsuitable to ask default Community Edition users to change. That design has been removed entirely.

Instead:

- The Graph API host is a **fixed constant** inside the "Validate & Build Request" Code node: `https://graph.facebook.com`. It is **not** a workflow input — accepting an arbitrary base URL as input would let a caller redirect this workflow's outbound request to any destination, which this workflow deliberately does not allow.
- The API **version** (`graphApiVersion`, e.g. `v21.0`) and your **phone-number ID** (`phoneNumberId`) are workflow inputs, each strictly validated by format before being used to build the URL — see [Workflow quality requirements](../README.md#workflow-quality-requirements) equivalent validation rules below.
- The full destination URL (`https://graph.facebook.com/{graphApiVersion}/{phoneNumberId}/messages`) is computed once, inside the Code node, and passed to the HTTP Request node as `={{ $json.url }}` — never as an environment-variable expression.

## Required credentials

**One:** an **HTTP Header Auth** credential (n8n credential type `httpHeaderAuth`), bound to the "Send Template Message" node, with:

- **Name:** `Authorization`
- **Value:** `Bearer YOUR_ACCESS_TOKEN` (your real Meta WhatsApp Cloud API access token)

**This credential is not included in the exported JSON.** No token, and no credential ID, is present anywhere in `whatsapp-template-message-sender.json` — after import, the "Send Template Message" node has no credential bound at all, by design. You must create this credential yourself in your own n8n instance and select it on that node. Never paste a live access token directly into a node parameter or expression — always use an n8n credential.

## Environment variables

**None.** See [Configuration design](#configuration-design) above — this workflow intentionally uses no environment variables. `graphApiVersion` and `phoneNumberId` are passed as regular workflow inputs by whatever workflow calls this one (e.g. hardcoded there, or itself sourced from that caller's own configuration — how the *caller* manages those values is outside this workflow's scope).

## Setup steps

1. Import `whatsapp-template-message-sender.json` into your n8n instance.
2. Open the "Send Template Message" node and create/select your **Header Auth** credential (see [Required credentials](#required-credentials)) — do not skip this, the node has none bound after import.
3. Review every node, especially the validation rules in "Validate & Build Request" and the Sticky Note's warnings.
4. You need an **approved** WhatsApp message template in your Meta Business account before this can succeed — template approval is entirely external to this workflow.
5. Call this as a **sub-workflow** from another n8n workflow using the Execute Workflow node, passing `recipientPhone`, `templateName`, `languageCode`, `graphApiVersion` (e.g. `"v21.0"` — check Meta's currently supported version yourself, this is not hardcoded as universally correct), `phoneNumberId`, and optionally `bodyParameters`.
6. Test with synthetic data against your own setup before relying on it — see [Test procedure](#test-procedure) for how this package itself was tested (against a mock server, not live Meta).

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched). **The real Meta API was never contacted.**

This package's design (fixed `https://graph.facebook.com` host) means the shipped workflow's own HTTP Request node could only ever target the real Meta API — so it was **never executed end-to-end** during testing. Instead, testing was split honestly into two parts:

**1. Validation and request-building logic — tested directly against the actual shipped (production, fixed-host) workflow:**

Every *rejection* scenario is, by construction, blocked by the IF node before the HTTP Request node ever runs — so these were safe to execute end-to-end against the real, fixed-host workflow, and confirmed (by inspecting each execution's node history) that "Send Template Message" never ran:

- Missing/invalid recipient, invalid template name, invalid language code, oversized/excessive body parameters (4 original cases)
- Missing Graph API version, invalid Graph API version format, missing phone-number ID, invalid phone-number ID (4 new cases)

All 9 returned `{"status":"rejected","httpStatus":null,"providerMessageId":null}` with **zero** node executions reaching "Send Template Message", confirmed via each execution's recorded node history.

The URL- and request-body-building logic for *valid* input was verified with a standalone Node.js unit test (not executed through n8n's HTTP node at all) confirming, for example, that `graphApiVersion: "v21.0"` + `phoneNumberId: "000000000000000"` builds `https://graph.facebook.com/v21.0/000000000000000/messages`.

**2. HTTP behavior — tested only through a temporary, uncommitted mock-copy workflow:**

A **temporary test-only duplicate** of this workflow was created inside the same isolated n8n instance, identical in every respect except that its "Validate & Build Request" Code node targeted a local mock HTTP server (`http://127.0.0.1:8765`, Python standard library, not part of this repository) instead of the fixed production host. This copy was never exported and never committed. All HTTP-response-handling scenarios were tested only through it:

- Successful send → `{"status":"sent","httpStatus":200,"providerMessageId":"..."}`
- Send with `bodyParameters` → confirmed the outbound request body's `template.components` structure, and that the request path matched the configured `graphApiVersion`/`phoneNumberId` (e.g. `/v21.0/000000000000000/messages`)
- Provider HTTP 400 → `provider_rejected`. 401 → `auth_error`. 429 → `rate_limited`. 500 → `provider_error`. All with the real `httpStatus` echoed back.
- Simulated timeout (mock server delayed past the node's 5-second timeout) → `timeout`, httpStatus null.
- Confirmed **no automatic retry**: for every failing scenario, the mock server's request log recorded exactly one request.

**Every response, across every scenario in both parts, was confirmed programmatically to contain exactly the three documented fields.**

**Clean re-import:** exported the production (fixed-host, no test-only modifications) workflow using n8n's official CLI (`n8n export:workflow`); stripped instance-specific metadata and the credential reference entirely; imported the sanitized `.json` into a **second, completely clean** n8n instance (fresh empty data directory, new SQLite database) using `n8n import:workflow`; confirmed the imported node had no credential bound, then created a new synthetic credential there and bound it manually. Rebuilt both a fresh production harness and a fresh temporary mock-copy on the clean instance, and re-ran **all 17** of the above test scenarios (9 rejection + 8 HTTP-behavior) — every result was identical to the first instance, confirmed programmatically. All temporary instances, credentials, harness/mock-copy workflows, the mock server, and associated directories/processes were removed after evidence was collected.

**To be explicit about what was, and was not, verified:** the shipped, fixed-host workflow was verified for correct rejection behavior end-to-end, and its URL/request-building logic was verified in isolation. It was **never executed against the real Meta API**, and it was **never executed against the local mock server either** — only the separate, non-shipped, host-swapped copy was. The two are structurally identical except for that one line, but this document does not claim the shipped workflow was itself run against the mock, nor against Meta.

All test data was synthetic: fake phone numbers (`1000000000x`), a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, and a local mock server — no real Meta credentials, phone numbers, or templates were used anywhere.

## Known limitations

- **No delivery-status tracking.** A `status: "sent"` result means the Graph API *accepted* the send request — it does not mean the message was delivered or read. Tracking that requires a separate webhook subscription to Meta's message-status callbacks.
- **No automatic retries, intentionally.** A failed send (rate limit, timeout, server error) is reported as a controlled failure and left for the caller to decide whether/how to retry — this avoids accidentally sending the same WhatsApp message twice.
- **`graphApiVersion` is not validated against Meta's actual list of supported versions** — only its *format* (e.g. `v21.0`) is checked. Meta deprecates old API versions on its own schedule; keep the version your calling workflow passes in up to date yourself.
- **Template approval, rate limits, and billing are entirely external.** This workflow assumes the template is already approved in your Meta Business account; it does not manage template creation/approval, and has no awareness of Meta's messaging rate limits, conversation-based pricing, or account-level billing.
- **n8n's own execution history may retain input data.** Depending on your instance's execution-data retention settings, the recipient phone number and body parameters passed into an execution may be stored locally by n8n itself (a platform behavior, not something this workflow controls) — review your own instance's settings if that matters for your data-handling policy.
- Only the immediate HTTP response is classified; this workflow does not poll or wait for anything beyond the initial send request.

## Data handled

Reads `recipientPhone`, `templateName`, `languageCode`, `graphApiVersion`, `phoneNumberId`, and `bodyParameters` from its caller, only in memory during a single execution, to build and send the template-message request. It makes exactly one outbound HTTP call (to `https://graph.facebook.com/{graphApiVersion}/{phoneNumberId}/messages`) when input is valid. Its response never includes the recipient number, message/template content, the access token, raw provider response headers, or internal error details/stack traces — only `status`, `httpStatus`, and `providerMessageId`. It does not write to any database and does not track message state beyond the single send response. See [Known limitations](#known-limitations) regarding n8n's own execution-history retention.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
