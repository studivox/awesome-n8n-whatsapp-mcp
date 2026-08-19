# WhatsApp Inbound Support Router

## What it does

Receives an inbound WhatsApp Business Cloud API-style webhook payload (as an HTTP POST), safely extracts the first inbound text message, and classifies it into one of four routes — `appointments`, `billing`, `support`, or `general` — using deterministic, word/phrase-aware keyword matching. Delivery-status-only events (message delivered/read receipts) and malformed or incomplete payloads are safely ignored rather than causing an error. Every execution path — classified or ignored — returns exactly the same three-field structured JSON result: `route`, `messageType`, `status`. No message content, phone numbers, message IDs, or internal error details are ever included in the response.

This is a **routing template, not a production-ready system**. It is a tested starting point for building your own message-routing logic — not a finished, deployable product. It also does **not** implement Meta's webhook verification handshake — see [Known limitations](#known-limitations) before pointing anything real at it.

## Real business use case

A common first step for any WhatsApp-based customer channel: deciding *where* an inbound message should go (a booking flow, a billing/finance queue, a support queue, or a general inbox) before any further automation or human handoff happens. This workflow implements only that routing decision, so it can sit in front of whatever appointment, billing, or support system a business already uses.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Webhook** (`n8n-nodes-base.webhook`, v2.1) — receives the inbound POST request.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas documentation warning that this is POST-only and not Meta-verification-ready; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — normalizes the payload and performs the word/phrase-aware classification.
- **Respond to Webhook** (`n8n-nodes-base.respondToWebhook`, v1.5) — returns the structured JSON result.

All node types are part of n8n core — no community nodes required.

## Required credentials

**None.** This workflow does not use any n8n credential of any kind — it only reads the incoming webhook body and returns a JSON response. There is nothing to configure in n8n's Credentials screen for this workflow to run.

## Environment variables

**None required by the workflow itself.** If you later add Meta's required GET verification handshake and signature validation (see [Known limitations](#known-limitations)), treat any verify token or app secret as a placeholder in your own documentation, e.g. `YOUR_WEBHOOK_VERIFY_TOKEN`, never a real value.

## Setup steps

1. Import `whatsapp-inbound-support-router.json` into your n8n instance.
2. Open the workflow and review every node — especially the Code node's classification keywords and the Sticky Note's warnings.
3. Adjust the keyword lists in the "Normalize, Validate & Classify" node to match your business's vocabulary and language(s) (see [Known limitations](#known-limitations)).
4. Activate the workflow. Note the generated webhook URL (path: `whatsapp-inbound-support-router`).
5. **Test it with any synthetic HTTP client** (curl, Postman, etc.) sending POST requests with test payloads — see [Test procedure](#test-procedure) for real examples.
6. **Do not point Meta's WhatsApp Cloud API webhook configuration directly at this URL.** This workflow only implements the POST message-handling side. Direct Meta integration additionally requires: (a) a separate endpoint handling Meta's `GET` verification handshake (`hub.mode`/`hub.verify_token`/`hub.challenge`), and (b) `X-Hub-Signature-256` signature validation on every inbound POST, before the URL is ever exposed publicly. Neither is included here — see [`whatsapp-webhook-security-gateway`](whatsapp-webhook-security-gateway.md) for a verified implementation of both.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched).

1. Created the workflow through n8n's own REST API (validated by n8n's node/type schema, not hand-written).
2. Activated it and sent 10 synthetic webhook requests: an appointment request, a billing/invoice question, a technical/help request, an unmatched general message, a delivery-status-only event, a malformed/incomplete payload, plus 4 classification regression cases specifically proving substring false-positives are fixed:
   - `"Facebook page"` → `general` (not misclassified as `appointments` via "book")
   - `"billion users"` → `general` (not misclassified as `billing` via "bill")
   - `"I want to book an appointment"` → `appointments`
   - `"I have a billing problem"` → `billing`

   All 10 returned the expected `route`/`messageType`/`status` with HTTP 200, and every response contained **exactly** the three documented fields (verified programmatically, no extra `reason` or other field on any path).
3. Ran 2 additional edge cases (empty JSON body, non-text/image message) — both handled safely without error.
4. Stopped the instance and exported the workflow using n8n's official CLI (`n8n export:workflow`).
5. Stripped instance-specific metadata (ownership/project IDs, version bookkeeping) from the raw CLI export to produce the portable `.json` file in this repository — matching what a normal editor "Download" produces.
6. Spun up a **second, completely clean** n8n instance (fresh empty data directory, new SQLite database) and imported the sanitized `.json` file using n8n's official CLI (`n8n import:workflow`).
7. Re-ran all 10 synthetic test cases plus both edge cases against the clean, re-imported instance — every result was identical to step 2/3, byte-for-byte, confirmed programmatically.
8. Removed all temporary instances, directories, and processes created for this test after evidence was collected.

All test payloads used only synthetic values: fake phone numbers (`YOUR_TEST_PHONE_NUMBER`, `1000000000x`), fake WhatsApp message IDs (`wamid.TESTxxxx`), and generic test message text — no real customer data of any kind.

## Known limitations

- **No webhook authenticity/signature verification, and no Meta GET handshake.** This workflow is POST-only: it does not validate Meta's `X-Hub-Signature-256` header, and it does not implement the webhook verify-token `GET` handshake Meta requires before it will send you any traffic. **Do not point Meta's webhook configuration directly at this URL as-is** — see [`whatsapp-webhook-security-gateway`](whatsapp-webhook-security-gateway.md) for a verified GET-verification and POST-signature-validation implementation, and only expose this publicly once both are in place in front of it.
- **Classification is deterministic word/phrase matching, not AI/ML.** It checks a fixed list of English words/phrases (e.g. "invoice", "appointment", "help", "not working") using word-boundary-aware matching — so "book" won't match inside "Facebook" and "bill" won't match inside "billion", but it still won't understand meaning, synonyms, other languages, or messages mixing multiple topics. Businesses must adapt the keyword lists (and add other languages) to their own needs before relying on this.
- Only the **first** text message in a payload is processed; only `type: "text"` messages are classified — other message types (image, audio, location, interactive replies, etc.) are safely routed to an "ignored" result, not classified.
- No retry, logging, or alerting is implemented beyond n8n's own execution history.

## Data handled

The workflow reads the inbound webhook payload (whatever WhatsApp Cloud API sends: sender phone number, message text, message ID, timestamps) only in memory during a single execution, to produce the routing decision. It does **not** call any external API, does **not** write to a database, and does **not** send any outbound message. Its response never includes message content, phone numbers, message IDs, or internal error details — only `route`, `messageType`, and `status`. It never stores customer data itself — though note that n8n's own execution-history feature (a platform setting, not something this workflow controls) may retain a copy of each execution's input/output locally depending on your instance's execution-data retention configuration; review your own instance's settings if that matters for your data-handling policy.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
