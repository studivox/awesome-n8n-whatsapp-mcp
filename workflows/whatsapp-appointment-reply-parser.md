# WhatsApp Appointment Reply Parser

## What it does

Receives an inbound WhatsApp Business Cloud API-style webhook payload (as an HTTP POST), safely extracts the first inbound text message, and parses it as a reply to an appointment message — classifying it as `confirmed`, `cancelled`, `reschedule_requested`, or `manual_review` using deterministic, word/phrase-aware keyword matching. Conflicting signals (e.g. both "confirm" and "cancel" present) or no recognizable signal at all both route to `manual_review` rather than guessing. Delivery-status-only events, malformed/incomplete payloads, and non-text messages are safely ignored. Every execution path returns exactly the same three-field structured JSON result: `action`, `messageType`, `status`.

**This is a reply parser only — not a complete appointment-management system.** It does **not** update any calendar or database, does **not** send any WhatsApp message, and does **not** claim that an appointment was actually changed. It only reports what the customer's reply seems to say; acting on that (updating a booking system, sending a follow-up) is a separate integration left to whoever uses this template. It also does **not** implement Meta's webhook verification handshake — see [Known limitations](#known-limitations) before pointing anything real at it.

## Real business use case

Once a business sends an appointment confirmation/reminder message (see the [planned appointment-confirmation workflow](../docs/ROADMAP.md)), customers reply in free text — "yes", "can't make it", "can we move it?", or something unrelated. This workflow turns that free-text reply into a structured signal a human or another automation can act on, without guessing on ambiguous or conflicting replies.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Webhook** (`n8n-nodes-base.webhook`, v2.1) — receives the inbound POST request.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas documentation of scope and limitations; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — normalizes the payload and performs the word/phrase-aware classification.
- **Respond to Webhook** (`n8n-nodes-base.respondToWebhook`, v1.5) — returns the structured JSON result.

All node types are part of n8n core — no community nodes required.

## Required credentials

**None.** This workflow does not use any n8n credential of any kind — it only reads the incoming webhook body and returns a JSON response.

## Environment variables

**None required by the workflow itself.** If you later add Meta's required GET verification handshake and signature validation (see [Known limitations](#known-limitations)), treat any verify token or app secret as a placeholder in your own documentation, e.g. `YOUR_WEBHOOK_VERIFY_TOKEN`, never a real value.

## Setup steps

1. Import `whatsapp-appointment-reply-parser.json` into your n8n instance.
2. Open the workflow and review every node — especially the Code node's keyword lists and the Sticky Note's scope notes.
3. Adjust the keyword lists in the "Normalize, Validate & Classify Reply" node to match your business's vocabulary and language(s) (see [Known limitations](#known-limitations)).
4. Activate the workflow. Note the generated webhook URL (path: `whatsapp-appointment-reply-parser`).
5. **Test it with any synthetic HTTP client** (curl, Postman, etc.) sending POST requests with test payloads — see [Test procedure](#test-procedure) for real examples.
6. **Do not point Meta's WhatsApp Cloud API webhook configuration directly at this URL.** Direct Meta integration additionally requires a separate endpoint handling Meta's `GET` verification handshake and `X-Hub-Signature-256` signature validation on every inbound POST — see [`whatsapp-webhook-security-gateway`](whatsapp-webhook-security-gateway.md) for a verified implementation of both.
7. Decide what happens with each `action` value in *your* system (e.g. `confirmed` → mark the booking confirmed in your calendar tool; `manual_review` → alert a human) — this workflow deliberately stops at reporting the parsed result.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched).

1. Created the workflow through n8n's own REST API (validated by n8n's node/type schema, not hand-written).
2. Activated it and sent 8 synthetic webhook requests:
   - `"Yes, I confirm my appointment"` → `confirmed`
   - `"Please cancel my appointment"` → `cancelled`
   - `"Can we move it to another day?"` → `reschedule_requested`
   - `"Thanks for the update"` (ambiguous, no signal) → `manual_review`
   - `"Yesterday I had a great trip"` (misleading substring: "yes" inside "yesterday" must not match `confirmed`) → `manual_review`
   - A delivery-status-only payload → ignored (`status_event`)
   - A malformed/incomplete payload → ignored (`malformed`)
   - A non-text (image) message → ignored (`image`)

   All 8 returned HTTP 200 with the expected `action`/`messageType`/`status`, and every response contained **exactly** the three documented fields (verified programmatically).
3. Stopped the instance and exported the workflow using n8n's official CLI (`n8n export:workflow`).
4. Stripped instance-specific metadata (ownership/project IDs, version bookkeeping) from the raw CLI export to produce the portable `.json` file in this repository.
5. Spun up a **second, completely clean** n8n instance (fresh empty data directory, new SQLite database) and imported the sanitized `.json` file using n8n's official CLI (`n8n import:workflow`).
6. Re-ran all 8 synthetic test cases against the clean, re-imported instance — every result was identical to step 2, confirmed programmatically.
7. Removed all temporary instances, directories, and processes created for this test after evidence was collected.

All test payloads used only synthetic values: fake phone numbers (`YOUR_TEST_PHONE_NUMBER`, `1000000000x`), fake WhatsApp message IDs (`wamid.RTESTxx`), and generic test message text — no real customer data of any kind.

## Known limitations

- **No webhook authenticity/signature verification, and no Meta GET handshake.** This workflow is POST-only. **Do not point Meta's webhook configuration directly at this URL as-is** — see [`whatsapp-webhook-security-gateway`](whatsapp-webhook-security-gateway.md) for a verified GET-verification and POST-signature-validation implementation.
- **Classification is deterministic word/phrase matching, not AI/ML.** It recognizes a fixed list of English words/phrases with word-boundary-aware matching (so "yes" won't match inside "yesterday"), but it doesn't understand meaning, sarcasm, other languages, or nuanced replies. Businesses must adapt the keyword lists (and add other languages) before relying on this.
- **Conflicting signals always route to `manual_review`.** A reply matching more than one category (e.g. "I won't cancel, I confirm") is deliberately never guessed at — this favors safety over decisiveness.
- **This workflow takes no action beyond classification.** It does not update a calendar, does not update a database, and does not send any WhatsApp message — including no confirmation of receipt back to the customer. All of that is left for the business to build on top of the `action` value this returns.
- Only the **first** text message in a payload is processed; only `type: "text"` messages are classified — other message types are safely routed to an "ignored" result.

## Data handled

The workflow reads the inbound webhook payload (sender phone number, message text, message ID, timestamps) only in memory during a single execution, to produce the parsed result. It does **not** call any external API, does **not** write to a database, and does **not** send any outbound message. Its response never includes message content, phone numbers, message IDs, or internal error details — only `action`, `messageType`, and `status`. It never stores customer data itself — though n8n's own execution-history feature (a platform setting, not something this workflow controls) may retain a copy of each execution's input/output locally depending on your instance's execution-data retention configuration.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
