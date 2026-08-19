# WhatsApp Delivery Status Parser

## What it does

Receives an inbound WhatsApp Business Cloud API-style webhook payload (as an HTTP POST) and parses a message **delivery-status** event (`sent`, `delivered`, `read`, `failed`, or any future status value Meta might add) into a small, safe summary. Delivery-status-only events are the norm for this workflow's input; malformed payloads, empty payloads, and payloads that contain an inbound *message* but no status event are all safely ignored rather than causing an error. Every execution path returns exactly the same five-field structured JSON result: `providerMessageId`, `deliveryStatus`, `eventTimestamp`, `errorCode`, `processingStatus`.

**This is a parser only.** It does **not** store anything, does **not** send any WhatsApp message, does **not** update a calendar/database/CRM, and provides no end-to-end delivery-tracking system on its own — it only turns one webhook call into a structured result. It also does **not** implement Meta's webhook verification handshake — see [Known limitations](#known-limitations) before exposing it anywhere.

## Real business use case

Once a business sends a WhatsApp message (a template, a reply, anything), Meta calls back with status webhooks as that message moves through `sent` → `delivered` → `read` (or `failed`). Turning those webhook calls into a clean, minimal signal — without accidentally logging a customer's phone number or a raw diagnostic payload — is the first step toward any delivery-tracking or retry system, without this workflow itself being that system.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Webhook** (`n8n-nodes-base.webhook`, v2.1) — receives the inbound POST request.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas documentation of scope and limitations; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — parses the payload and builds the five-field result.
- **Respond to Webhook** (`n8n-nodes-base.respondToWebhook`, v1.5) — returns the structured JSON result.

All node types are part of n8n core — no community nodes required.

## Required credentials

**None.** This workflow does not use any n8n credential of any kind — it only reads the incoming webhook body and returns a JSON response.

## Environment variables

**None.**

## Setup steps

1. Import `whatsapp-delivery-status-parser.json` into your n8n instance.
2. Open the workflow and review the Code node and the Sticky Note's warnings.
3. Activate the workflow. Note the generated webhook URL (path: `whatsapp-delivery-status-parser`).
4. **Test it with any synthetic HTTP client** (curl, Postman, etc.) sending POST requests with test payloads — see [Test procedure](#test-procedure) for real examples.
5. **Do not expose this workflow's URL directly to Meta without a verified gateway in front of it.** This workflow implements no GET verification handshake and no `X-Hub-Signature-256` signature validation — put a gateway that handles both in front of it before pointing any real Meta webhook subscription here.
6. Decide what happens with each `deliveryStatus`/`processingStatus` value in *your* system — this workflow deliberately stops at reporting the parsed result; it stores and sends nothing itself.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched).

1. Created the workflow through n8n's own REST API (validated by n8n's node/type schema, not hand-written).
2. Activated it and sent 10 synthetic webhook requests:
   - `sent`, `delivered`, `read` status events → parsed correctly, each with the matching `deliveryStatus` and `providerMessageId`.
   - `failed` status event with a synthetic `errors[0].code` **and** deliberately sensitive `title`/`message`/`error_data` text → parsed with `errorCode` set to the numeric code only; confirmed the sensitive text never appeared anywhere in the response.
   - An unrecognized/future status value (`"reacted"`, not one of sent/delivered/read/failed) → passed through safely as `deliveryStatus: "reacted"`, `processingStatus: "parsed"` — no crash, no rejection.
   - A status event missing its provider message ID → `processingStatus: "ignored"`, all other fields `null`.
   - A malformed/unrecognized payload shape → ignored.
   - An empty payload (`{}`) → ignored.
   - A payload containing an inbound *message* but no status event → ignored.
   - A payload with **two** status entries → confirmed only the **first** was reflected in the output, proving the documented "first event only" limitation.

   All 10 returned HTTP 200 with the expected fields, and every response was confirmed programmatically to contain **exactly** the five documented fields — never a recipient phone number, message content, the raw webhook payload, or the sensitive error `title`/`message`/`error_data` text.
3. Stopped the instance and exported the workflow using n8n's official CLI (`n8n export:workflow`).
4. Stripped instance-specific metadata (ownership/project IDs, version bookkeeping) from the raw CLI export to produce the portable `.json` file in this repository.
5. Spun up a **second, completely clean** n8n instance (fresh empty data directory, new SQLite database) and imported the sanitized `.json` file using n8n's official CLI (`n8n import:workflow`).
6. Re-ran all 10 synthetic test cases against the clean, re-imported instance — every result was identical to step 2, confirmed programmatically.
7. Removed all temporary instances, directories, and processes created for this test after evidence was collected.

All test payloads used only synthetic values: fake WhatsApp message IDs (`wamid.DSTESTxxxx`), fake recipient IDs, and a deliberately sensitive-looking (but entirely fabricated) error message/detail string used specifically to confirm it does **not** leak into the output — no real customer data of any kind.

## Known limitations

- **No webhook authenticity/signature verification, and no Meta GET handshake.** This workflow is POST-only and trusts whatever is sent to it. **Do not expose it directly to Meta without a verified gateway in front of it** that handles the GET verification handshake and `X-Hub-Signature-256` validation — without that, anyone who finds the URL can send it arbitrary payloads.
- **Only the first status event in a payload is processed.** If Meta batches multiple status updates into a single webhook call, every entry after the first is silently not processed by this workflow. A production system needs to either configure Meta/its gateway to avoid batching, or extend this workflow to loop over all entries.
- **No end-to-end delivery tracking.** This workflow does not persist status history, does not correlate a `failed` event back to the original send request, and does not retry or alert on anything — it is a single-event parser, not a tracking system.
- **Unknown status values are passed through, not validated against a fixed list.** This is deliberate (so a future Meta status addition doesn't break this workflow) but means a caller relying on `deliveryStatus` should not assume it is always one of `sent`/`delivered`/`read`/`failed`.
- **n8n's own execution history may retain input data.** Depending on your instance's execution-data retention settings, the full inbound webhook payload (including whatever Meta sent) may be stored locally by n8n itself (a platform behavior, not something this workflow controls) — review your own instance's settings if that matters for your data-handling policy.

## Data handled

Reads the inbound webhook payload (whatever WhatsApp Cloud API sends: recipient ID, message ID, status, timestamp, and — for failures — an error object) only in memory during a single execution, to produce the parsed result. It does **not** call any external API, does **not** write to a database, and does **not** send any outbound message. Its response never includes a recipient phone number, message content, the raw webhook payload, or raw provider error text/diagnostic detail — only `providerMessageId` (an opaque message identifier, not personal data), `deliveryStatus`, `eventTimestamp`, `errorCode` (a bare numeric/string code), and `processingStatus`. It never stores anything itself — though n8n's own execution-history feature (a platform setting, not something this workflow controls) may retain a copy of each execution's input/output locally depending on your instance's execution-data retention configuration.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
