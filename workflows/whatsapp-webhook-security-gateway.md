# WhatsApp Webhook Security Gateway

## What it does

Handles the two things every direct Meta WhatsApp Cloud API webhook integration needs before any customer data should be trusted: Meta's **GET verification handshake** and **POST `X-Hub-Signature-256` signature validation**. It is meant to sit in front of the other workflows in this repository (e.g. [`whatsapp-inbound-support-router`](whatsapp-inbound-support-router.md), [`whatsapp-delivery-status-parser`](whatsapp-delivery-status-parser.md)) so that *those* workflows only ever receive requests that have already been cryptographically verified as genuinely coming from Meta.

- **GET requests** (Meta's subscription verification): checks `hub.mode` is `subscribe` and securely compares `hub.verify_token` against your configured secret. Responds with the exact `hub.challenge` value as plain text only on success; otherwise returns `403 Forbidden`.
- **POST requests** (actual webhook deliveries): reads the raw request body exactly as sent, computes an HMAC-SHA256 over those exact bytes using your Meta app secret, and compares it — in constant time — against the `X-Hub-Signature-256` header Meta sent. Only a request with a valid signature reaches a clearly marked "verified" attachment point; everything else is rejected with `401 Unauthorized` before any further processing.

**Neither secret (verify token or app secret) ever appears as a plain value anywhere in this workflow's exported JSON.** Both live only in two separate n8n **Crypto** credentials, used as HMAC keys — see [Security design](#security-design) for exactly how, and why this was verified experimentally rather than assumed.

## Real business use case

Every other workflow in this repository that receives inbound WhatsApp webhooks (delivery-status events, inbound messages) explicitly documents that it does **not** implement Meta's authenticity checks and must not be exposed directly to Meta. This workflow is that missing piece: a single, reusable gateway that verifies a request is genuinely from Meta before anything downstream (routing logic, database writes, business logic) ever sees it.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Webhook** (`n8n-nodes-base.webhook`, v2.1) — used twice, once for `GET` (verification) and once for `POST` (deliveries), both on the **same path** so Meta's single configured callback URL works for both.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — two, documenting the GET and POST branches on-canvas.
- **Crypto** (`n8n-nodes-base.crypto`, v2) — used twice, in `Hmac` mode, each bound to its own **Crypto** credential (see below). This is the only n8n core node that can use a credential-stored secret as an HMAC key without that secret ever being readable from a workflow parameter or expression.
- **Code** (`n8n-nodes-base.code`, v2) — used twice, for the GET-verification decision and the POST-signature decision. Contains only a hand-rolled constant-time hex-string comparison (see [Security design](#security-design) for why `require('crypto')` was not used).
- **IF** (`n8n-nodes-base.if`, v2.3) — used twice, gating both branches before anything downstream runs.
- **NoOp** (`n8n-nodes-base.noOp`, v1) — one node, named `VERIFIED - Attach Downstream Processing Here`, marking exactly where a verified POST request's data becomes available for further processing.
- **Respond to Webhook** (`n8n-nodes-base.respondToWebhook`, v1.5) — four, one for each of GET-success / GET-rejected / POST-success / POST-rejected.

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise feature.

## Security design

This design was **not assumed** — every mechanism below was verified experimentally against a live n8n v2.35.4 instance before being used. See [Test procedure](#test-procedure) for exactly how.

### Secret storage: the `crypto` credential type

n8n's core **Crypto** node declares a required credential of type `crypto`, which has an `Hmac Secret` field. When the Crypto node's action is `Hmac`, it reads this field internally (via `this.getCredentials('crypto')`, a mechanism only available to the node's own compiled implementation) and uses it as the HMAC key — the secret is never exposed to node parameters, expressions, or the Code node. This repository uses **two separate `crypto` credentials**: one holding your Meta **app secret** (for POST signature validation), one holding your **verify token** (for the GET handshake). Neither secret is present anywhere in `whatsapp-webhook-security-gateway.json` — confirmed by grepping the exported file.

**Important finding: n8n's Code node cannot access any credential, of any type, under any circumstance.** Its node-type definition declares no `credentials` field at all (unlike Webhook, Crypto, HTTP Request, and Respond to Webhook, which all do). This was verified directly, not assumed — it rules out the simplest possible design ("just compare the token in a Code node") and shaped everything below.

### GET verification without ever reading the verify token

Because a Code node cannot read the verify-token credential, and the Crypto node can only use it as an HMAC *key* (not read it as plain data), a direct `incoming === secret` comparison is not achievable with core nodes. Instead, this workflow uses an HMAC **commitment** scheme:

1. **At setup time** (once, outside n8n — see [Setup steps](#setup-steps)), you compute `EXPECTED_COMMITMENT = HMAC-SHA256(key = your verify token, message = your verify token)`. This is a one-way, non-reversible value — knowing it does not reveal the verify token — so it's safe to paste into the `Evaluate GET Verification` Code node as a plain constant.
2. **At request time**, the `Hash GET Verify Token` Crypto node computes `HMAC-SHA256(key = verify token from credential, message = incoming hub.verify_token)`.
3. If the incoming token equals the real verify token, step 2's message equals step 1's message (both are the real token), so step 2's result equals `EXPECTED_COMMITMENT` exactly — proving a match without the Code node ever reading the secret. If the incoming token is wrong, the two HMACs differ (cryptographically, for all practical purposes).

This was verified to work correctly — see [Test procedure](#test-procedure) — with the correct token producing a match and any incorrect token producing a mismatch.

### POST signature validation over exact raw bytes

The `POST Trigger` Webhook node has its **Raw Body** option enabled. This was verified experimentally to preserve the exact request bytes (confirmed byte-for-byte, including deliberately unusual whitespace) in a binary property, available *alongside* n8n's normal parsed-JSON body — so this workflow never signs a re-serialized/re-parsed reconstruction of the payload, which would silently break on any whitespace difference from what Meta actually sent. The `Compute POST Body HMAC` Crypto node hashes that binary data directly (`Hmac`, `Binary File` enabled), using the app-secret credential as the key. The result was verified to exactly match an independently-computed HMAC-SHA256 (Python's `hmac`/`hashlib`) of the same bytes and key.

### Constant-time comparison without `require('crypto')`

`require('crypto')` **is blocked by default** in the Code node sandbox (confirmed experimentally: `Module 'crypto' is disallowed`) — enabling it requires the instance-wide `NODE_FUNCTION_ALLOW_BUILTIN` environment variable, which this project deliberately avoids for the same reason the [WhatsApp template message sender](whatsapp-template-message-sender.md) avoids `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`: it shouldn't require weakening a shared instance-wide security default, and it wouldn't work unmodified on n8n Cloud. Instead, both comparisons use a small hand-written constant-time hex-string comparison (XOR-accumulate over every character, no early exit) — equivalent in effect to `crypto.timingSafeEqual` for same-length hex digests, without needing the blocked module.

### Structural rejection

Both branches use an **IF** node between the security check and anything else. This was verified directly: for every rejected test case (wrong/missing token, wrong mode, wrong/missing/malformed/truncated signature, tampered body), the `VERIFIED - Attach Downstream Processing Here` node **never executed** — confirmed by inspecting each execution's recorded node history, not just the HTTP response.

## Required credentials

**Two**, both of n8n credential type **Crypto** (`crypto`):

| Credential | Bound to node | `Hmac Secret` field holds |
|---|---|---|
| e.g. "WhatsApp Gateway App Secret" | `Compute POST Body HMAC` | Your Meta app secret (from your Meta App's Basic Settings) |
| e.g. "WhatsApp Gateway Verify Token" | `Hash GET Verify Token` | Your chosen verify token (the same value you enter in Meta's webhook subscription config) |

**Neither credential is included in the exported JSON.** Both Crypto nodes have no credential bound after import, by design — you must create both credentials yourself and select them on the respective nodes.

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change of any kind is required — this workflow works unmodified on n8n Cloud and on a default Community Edition installation.

## Setup steps

1. Import `whatsapp-webhook-security-gateway.json` into your n8n instance.
2. Create your two **Crypto** credentials (see [Required credentials](#required-credentials)) with your real app secret and verify token, and bind them to the `Compute POST Body HMAC` and `Hash GET Verify Token` nodes respectively.
3. Compute your own `EXPECTED_COMMITMENT` value **outside n8n**, once:
   ```
   printf '%s' "YOUR_VERIFY_TOKEN" | openssl dgst -sha256 -hmac "YOUR_VERIFY_TOKEN" -r
   ```
   (replace `YOUR_VERIFY_TOKEN` with your real token in both places; take the hex digest printed, not the trailing filename marker `openssl` adds). This value is **not secret** — it cannot be reversed back into your verify token — but it is specific to your token and must match it exactly.
4. Open the `Evaluate GET Verification` Code node and replace the placeholder `EXPECTED_COMMITMENT` constant with the value from step 3.
5. Activate the workflow. Note the generated webhook URL (path: `whatsapp-webhook-gateway`) — the **same URL** handles both Meta's GET verification and POST deliveries.
6. Configure this URL as your WhatsApp Cloud API webhook callback URL in Meta's App Dashboard, along with your verify token (must match what you put in the credential and used to compute the commitment).
7. Attach your downstream processing (routing, parsing, persistence) after the `VERIFIED - Attach Downstream Processing Here` NoOp node — **never** add the customer payload to the `Respond POST Success` node's response body.
8. Test with a synthetic client first — see [Test procedure](#test-procedure) — before pointing a real Meta subscription at it.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched). The real Meta API was never contacted — all testing used a synthetic HTTP client (Python's `urllib`) sending crafted requests with independently-computed signatures/commitments.

**Feasibility investigation (before building anything):** confirmed experimentally, in this order:
1. The Code node has no `credentials` field in its node-type definition — it cannot read any credential.
2. The Crypto node's `Hmac` action requires a `crypto`-type credential and uses its `hmacSecret` field internally as the HMAC key, never exposed to parameters/expressions/output.
3. The Webhook node's **Raw Body** option preserves the exact request bytes (verified byte-for-byte against the original, including deliberately irregular whitespace).
4. The Crypto node's `Hmac` action, with **Binary File** enabled, can hash that raw binary data directly, and its output was confirmed to exactly match an independently-computed HMAC-SHA256 (Python `hmac`/`hashlib`) of the same bytes and key.
5. `require('crypto')` is blocked by default in the Code node sandbox (`Module 'crypto' is disallowed`) — ruling out `crypto.timingSafeEqual` without an instance-wide setting change.
6. The commitment scheme described in [Security design](#security-design) was built and confirmed to correctly distinguish a matching token from a non-matching one.

**Building and testing the real gateway (16 required tests, run before and after clean re-import):**

| # | Test | Result |
|---|---|---|
| 1 | Correct GET token and mode | `200`, body is the exact `hub.challenge` value |
| 2 | Wrong GET token | `403 Forbidden` |
| 3 | Missing GET token | `403 Forbidden` |
| 4 | Invalid mode | `403 Forbidden` |
| 5 | Valid POST signature | Reaches verified branch, `200 {"status":"received"}` |
| 6 | One-byte body modification (signature computed for the original) | `401 Forbidden` |
| 7 | JSON whitespace modification without recalculating signature | `401 Forbidden` |
| 8 | Wrong secret | `401 Forbidden` |
| 9 | Missing signature | `401 Forbidden` |
| 10 | Invalid prefix (`sha1=` instead of `sha256=`) | `401 Forbidden` |
| 11 | Truncated signature | `401 Forbidden` (no crash) |
| 12 | Upper/lowercase hex signature | `200`, verified (comparison lowercases the incoming hex first) |
| 13 | UTF-8 body (multi-byte characters) signed as exact bytes | `200`, verified |
| 14 | Rejected requests never reach downstream | Confirmed for every rejected case above by inspecting each execution's recorded node history — the `VERIFIED -...` node never ran |
| 15 | No secret in export/execution output | Confirmed: secret absent from the exported JSON; absent from every node's *output* in every execution. It does appear once in the GET trigger's raw recorded *input* when a request legitimately includes the correct token — this is Meta's own protocol (the token is echoed back in the query string) and n8n's standard trigger-input recording, identical to the already-documented behavior of every other webhook-based workflow in this repository |
| 16 | Clean import, secret rebinding, full repetition | All of the above re-run against a second, completely clean n8n instance after exporting via the official CLI, sanitizing, importing, and creating + binding two **new** credentials there — every result was identical |

Exported the workflow via n8n's official CLI (`n8n export:workflow`), stripped instance-specific metadata and both credential references entirely, imported the sanitized `.json` into a clean instance via `n8n import:workflow`, created new synthetic credentials there, manually bound them, and re-ran all 16 tests with identical results. Removed all temporary instances, credentials, and processes after evidence was collected.

All test data was synthetic: fake app secret and verify token values clearly labeled `SYNTHETIC_TEST_...`, a fake challenge string, and fabricated request bodies — no real Meta credentials or customer data anywhere.

## Known limitations

- **No replay or deduplication protection.** A validly-signed request replayed later (e.g. by an attacker who captured it, or by Meta's own retry behavior) will pass verification again. If your downstream processing isn't naturally idempotent, add your own deduplication (e.g. tracking `providerMessageId`/`entry.id` values you've already processed).
- **Downstream persistence is not implemented here.** This gateway only verifies and marks a request as safe to process — it does not store, forward, or act on anything itself.
- **The commitment scheme requires a manual one-time setup step** (computing `EXPECTED_COMMITMENT` outside n8n) — get this wrong and legitimate verification requests will fail; there is no way to auto-generate it from inside the workflow, since that would require exactly the credential-reading capability this design works around not having.
- **A compromised app secret or verify token defeats this entirely** — this gateway assumes both are kept genuinely secret; it has no additional layer of protection.
- **n8n's own execution history may retain the raw incoming request**, including the verify token when Meta correctly performs the GET handshake (Meta's own protocol echoes it back in the query string) — a platform behavior, not something this workflow controls. Review your own instance's execution-data retention settings.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads the raw GET query parameters and POST body/headers Meta sends. Computes HMAC digests using two credential-stored secrets, but never returns either secret in any response. The GET success response returns only the `hub.challenge` value Meta itself supplied in that same request. The POST success response returns only `{"status":"received"}` — never the customer payload. Rejected requests receive only `"Forbidden"` with an appropriate status code. Nothing is written to a database or sent onward by this workflow itself.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
