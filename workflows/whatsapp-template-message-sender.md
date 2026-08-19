# WhatsApp Template Message Sender

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook) that sends an approved WhatsApp Business Cloud API template message. It accepts `recipientPhone`, `templateName`, `languageCode`, and an optional `bodyParameters` array; validates all of them; builds the official WhatsApp template-message request body; and, only when valid, makes a single outbound HTTP POST to the Meta Graph API. It returns only non-sensitive delivery metadata: `status`, `httpStatus`, `providerMessageId`.

**This workflow sends a real WhatsApp message when connected to live credentials and a real Graph API URL.** Repository testing used a local mock HTTP server standing in for Meta — the real Meta API was never contacted during development or testing of this package.

**Not production-ready.** It provides no delivery-status tracking (it only reports whether the send *request* was accepted, not whether the message was actually delivered or read), and intentionally has no automatic retries.

## Real business use case

Sending appointment reminders, order updates, or invoice notifications via WhatsApp requires using a pre-approved message *template* (Meta does not allow free-form outbound business-initiated messages). This sub-workflow centralizes that "send an approved template" step — with input validation and a safe, controlled response — so other workflows (e.g. an appointment-reminder scheduler) can call it via n8n's Execute Workflow node instead of duplicating HTTP Request/auth logic.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the `recipientPhone` / `templateName` / `languageCode` / `bodyParameters` input contract.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope/limitation notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used four times: input validation + request building, and three distinct controlled-response builders (success, transport failure, rejected).
- **IF** (`n8n-nodes-base.if`, v2.3) — branches on whether input passed validation, before any HTTP request is made.
- **HTTP Request** (`n8n-nodes-base.httpRequest`, v4.5) — the actual outbound call to the Graph API, configured with `Never Error` + `Full Response` (so both success and non-2xx responses are classified by this workflow, not thrown as node errors) and `onError: continueErrorOutput` (so genuine transport/timeout failures route to a separate controlled branch instead of crashing the execution).

All node types are part of n8n core — no community nodes required.

## Required credentials

**One:** an **HTTP Header Auth** credential (n8n credential type `httpHeaderAuth`), bound to the "Send Template Message" node, with:

- **Name:** `Authorization`
- **Value:** `Bearer YOUR_ACCESS_TOKEN` (your real Meta WhatsApp Cloud API access token)

**This credential is not included in the exported JSON.** No token, and no credential ID, is present anywhere in `whatsapp-template-message-sender.json` — after import, the "Send Template Message" node has no credential bound at all, by design. You must create this credential yourself in your own n8n instance and select it on that node. Never paste a live access token directly into a node parameter or expression — always use an n8n credential.

## Environment variables

| Variable | Purpose | Example (not a live value) |
|---|---|---|
| `WHATSAPP_GRAPH_BASE_URL` | Base URL of the Meta Graph API | `https://graph.facebook.com` |
| `WHATSAPP_GRAPH_API_VERSION` | Graph API version path segment | `v21.0` (example only — **check Meta's currently supported version**; this is not hardcoded as universally correct and will need updating over time) |
| `WHATSAPP_PHONE_NUMBER_ID` | Your WhatsApp Business phone number ID | `YOUR_PHONE_NUMBER_ID` |

The HTTP Request node builds its URL as `{{ $env.WHATSAPP_GRAPH_BASE_URL }}/{{ $env.WHATSAPP_GRAPH_API_VERSION }}/{{ $env.WHATSAPP_PHONE_NUMBER_ID }}/messages`. Your n8n instance must have `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` (n8n's default blocks `$env` access in expressions) for this to resolve — see [Setup steps](#setup-steps).

## Setup steps

1. Import `whatsapp-template-message-sender.json` into your n8n instance.
2. Open the "Send Template Message" node and create/select your **Header Auth** credential (see [Required credentials](#required-credentials)) — do not skip this, the node has none bound after import.
3. Set `WHATSAPP_GRAPH_BASE_URL`, `WHATSAPP_GRAPH_API_VERSION`, and `WHATSAPP_PHONE_NUMBER_ID` as real environment variables on your n8n instance (not hardcoded in the workflow).
4. Ensure `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` is set on your instance, or the URL expression will fail with "access to env vars denied".
5. Review every node, especially the validation rules in "Validate & Build Request" and the Sticky Note's warnings.
6. You need an **approved** WhatsApp message template in your Meta Business account before this can succeed — template approval is entirely external to this workflow.
7. Call this as a **sub-workflow** from another n8n workflow using the Execute Workflow node, passing `recipientPhone`, `templateName`, `languageCode`, and optionally `bodyParameters`.
8. Test with synthetic data against your own setup before relying on it — see [Test procedure](#test-procedure) for how this package itself was tested (against a mock server, not live Meta).

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched). **The real Meta API was never contacted** — a temporary local mock HTTP server (Python standard library only, not part of this repository) stood in for the Graph API, and `WHATSAPP_GRAPH_BASE_URL` was pointed at it.

1. Created the sender sub-workflow through n8n's own REST API (validated by n8n's node/type schema, not hand-written), including the IF-node filter conditions, the HTTP Request node's response/error options, and the Execute Workflow Trigger's typed input schema.
2. Created a synthetic Header Auth credential (`Bearer SYNTHETIC_TEST_TOKEN_...`) and bound it to the "Send Template Message" node, on the isolated instance only.
3. Created a **temporary caller/harness workflow** (Webhook → Execute Workflow → Respond to Webhook, not part of this repository) to invoke the sub-workflow over HTTP for testing.
4. Ran the full test matrix against the mock server, driving each of its response scenarios via the mock's control endpoints:
   - Successful send → `{"status":"sent","httpStatus":200,"providerMessageId":"..."}`, with the provider message ID taken from the mock's response.
   - Send with `bodyParameters` → confirmed the outbound request body's `template.components` array had the correct `body`/`text` parameter structure.
   - Missing recipient, invalid recipient format, invalid template name, invalid language code, and oversized/excessive body parameters → all rejected **before** any HTTP request was made (confirmed via the mock server's request log being empty for each).
   - Provider HTTP 400 → `provider_rejected`. Provider HTTP 401 → `auth_error`. Provider HTTP 429 → `rate_limited`. Provider HTTP 500 → `provider_error`. All with the real `httpStatus` echoed back and no other data.
   - Simulated timeout (mock server delayed its response past the node's 5-second timeout) → `timeout`, with `httpStatus: null`.
   - Confirmed **no automatic retry**: for every failing scenario, the mock server's request log recorded exactly one request.
   - Every response, across every scenario, was confirmed programmatically to contain **exactly** the three documented fields — nothing else.
5. Stopped the instance and exported the sender sub-workflow (not the temporary harness) using n8n's official CLI (`n8n export:workflow`).
6. Stripped instance-specific metadata (ownership/project IDs, version bookkeeping) and the credential reference entirely (see [Required credentials](#required-credentials)) to produce the portable `.json` file in this repository.
7. Spun up a **second, completely clean** n8n instance (fresh empty data directory, new SQLite database) and imported the sanitized `.json` file using n8n's official CLI (`n8n import:workflow`).
8. Created a **new** synthetic credential on that clean instance and manually bound it to the imported node — proving the "create and bind after import" setup step actually works.
9. Rebuilt the temporary harness workflow on the clean instance and re-ran the entire test matrix — every result was identical to step 4, confirmed programmatically.
10. Removed all temporary instances, credentials, the harness workflows, the mock server, and all associated directories/processes after evidence was collected.

All test data was synthetic: fake phone numbers (`1000000000x`), a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, and a local mock server — no real Meta credentials, phone numbers, or templates were used anywhere.

## Known limitations

- **No delivery-status tracking.** A `status: "sent"` result means the Graph API *accepted* the send request — it does not mean the message was delivered or read. Tracking that requires a separate webhook subscription to Meta's message-status callbacks, not implemented here.
- **No automatic retries, intentionally.** A failed send (rate limit, timeout, server error) is reported as a controlled failure and left for the caller to decide whether/how to retry — this avoids accidentally sending the same WhatsApp message twice.
- **No Graph API version is hardcoded as "current."** `WHATSAPP_GRAPH_API_VERSION` is an environment variable you must set and keep up to date — Meta deprecates old API versions on its own schedule.
- **Template approval, rate limits, and billing are entirely external.** This workflow assumes the template is already approved in your Meta Business account; it does not manage template creation/approval, and has no awareness of Meta's messaging rate limits, conversation-based pricing, or account-level billing — all of that is the caller's/business's responsibility.
- **n8n's own execution history may retain input data.** Depending on your instance's execution-data retention settings, the recipient phone number and body parameters passed into an execution may be stored locally by n8n itself (a platform behavior, not something this workflow controls) — review your own instance's settings if that matters for your data-handling policy.
- Only the immediate HTTP response is classified; this workflow does not poll or wait for anything beyond the initial send request.

## Data handled

Reads `recipientPhone`, `templateName`, `languageCode`, and `bodyParameters` from its caller, only in memory during a single execution, to build and send the template-message request. It makes exactly one outbound HTTP call (to the URL built from your environment variables) when input is valid. Its response never includes the recipient number, message/template content, the access token, raw provider response headers, or internal error details/stack traces — only `status`, `httpStatus`, and `providerMessageId`. It does not write to any database and does not track message state beyond the single send response. See [Known limitations](#known-limitations) regarding n8n's own execution-history retention.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
