# WhatsApp Inbound Support Router

## What it does

Receives an inbound WhatsApp Business Cloud API webhook payload, safely extracts the first inbound text message, and classifies it into one of four routes — `appointments`, `billing`, `support`, or `general` — using deterministic keyword matching. Delivery-status-only events (message delivered/read receipts) and malformed or incomplete payloads are safely ignored rather than causing an error. The workflow returns a small structured JSON result (`route`, `messageType`, `status`) and takes no other action.

This is a **routing template, not a production-ready system**. It is a tested starting point for building your own message-routing logic — not a finished, deployable product.

## Real business use case

A common first step for any WhatsApp-based customer channel: deciding *where* an inbound message should go (a booking flow, a billing/finance queue, a support queue, or a general inbox) before any further automation or human handoff happens. This workflow implements only that routing decision, so it can sit in front of whatever appointment, billing, or support system a business already uses.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Webhook** (`n8n-nodes-base.webhook`, v2.1) — receives the inbound POST request.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas documentation only; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — normalizes the payload and performs the keyword classification.
- **Respond to Webhook** (`n8n-nodes-base.respondToWebhook`, v1.5) — returns the structured JSON result.

All node types are part of n8n core — no community nodes required.

## Required credentials

**None.** This workflow does not use any n8n credential of any kind — it only reads the incoming webhook body and returns a JSON response. There is nothing to configure in n8n's Credentials screen for this workflow to run.

## Environment variables

**None required by the workflow itself.** If you put this behind Meta's WhatsApp Cloud API webhook configuration, Meta requires a webhook **verify token** for the initial `GET` handshake — this workflow does not implement that handshake or any signature verification (see [Known limitations](#known-limitations)). If you add it, treat the verify token and any app secret as placeholders in documentation, e.g. `YOUR_WEBHOOK_VERIFY_TOKEN`, never a real value.

## Setup steps

1. Import `whatsapp-inbound-support-router.json` into your n8n instance.
2. Open the workflow and review every node — especially the Code node's classification keywords.
3. Adjust the keyword lists in the "Normalize, Validate & Classify" node to match your business's vocabulary and language(s) (see [Known limitations](#known-limitations)).
4. Add webhook signature verification before exposing this publicly (see [Known limitations](#known-limitations)) — this is not included.
5. Activate the workflow. Note the generated webhook URL (path: `whatsapp-inbound-support-router`).
6. Point your WhatsApp Cloud API webhook subscription (or a test client) at that URL.
7. Send a synthetic test message and confirm the JSON response matches the expected route.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched).

1. Created the workflow through n8n's own REST API (validated by n8n's node/type schema, not hand-written).
2. Activated it and sent 6 synthetic webhook requests covering: an appointment request, a billing/invoice question, a technical/help request, an unmatched general message, a delivery-status-only event, and a malformed/incomplete payload. All 6 returned the expected `route`/`status` with HTTP 200.
3. Ran 2 additional edge cases (empty JSON body, non-text/image message) — both handled safely without error.
4. Stopped the instance and exported the workflow using n8n's official CLI (`n8n export:workflow`).
5. Stripped instance-specific metadata (ownership/project IDs, version bookkeeping) from the raw CLI export to produce the portable `.json` file in this repository — matching what a normal editor "Download" produces.
6. Spun up a **second, completely clean** n8n instance (fresh empty data directory, new SQLite database) and imported the sanitized `.json` file using n8n's official CLI (`n8n import:workflow`).
7. Re-ran all 6 synthetic test cases against the clean, re-imported instance — all 6 returned identical, correct results.
8. Removed all temporary instances, directories, and processes created for this test after evidence was collected.

All test payloads used only synthetic values: fake phone numbers (`YOUR_TEST_PHONE_NUMBER`, `1000000000x`), fake WhatsApp message IDs (`wamid.TESTxxxx`), and generic test message text — no real customer data of any kind.

## Known limitations

- **No webhook authenticity/signature verification.** This workflow does not validate Meta's `X-Hub-Signature-256` header or implement the webhook verify-token `GET` handshake. **You must add this before exposing the workflow's URL publicly** — without it, anyone who finds the URL can send it arbitrary payloads.
- **Classification is deterministic keyword matching, not AI/ML.** It checks for a fixed list of English substrings (e.g. "invoice", "appointment", "help"). It will misclassify messages that don't use those words, messages in other languages, and messages mixing multiple topics. Businesses must adapt the keyword lists (and add other languages) to their own needs before relying on this.
- Only the **first** text message in a payload is processed; only `type: "text"` messages are classified — other message types (image, audio, location, interactive replies, etc.) are safely routed to an "ignored" result, not classified.
- No retry, logging, or alerting is implemented beyond n8n's own execution history.

## Data handled

The workflow reads the inbound webhook payload (whatever WhatsApp Cloud API sends: sender phone number, message text, message ID, timestamps) only in memory during a single execution, to produce the routing decision. It does **not** call any external API, does **not** write to a database, and does **not** send any outbound message. It never stores customer data itself — though note that n8n's own execution-history feature (a platform setting, not something this workflow controls) may retain a copy of each execution's input/output locally depending on your instance's execution-data retention configuration; review your own instance's settings if that matters for your data-handling policy.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
