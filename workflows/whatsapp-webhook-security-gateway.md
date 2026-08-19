# WhatsApp Webhook Security Gateway

## What it does

Handles the two things every direct Meta WhatsApp Cloud API webhook integration needs before any customer data should be trusted: Meta's **GET verification handshake** and **POST `X-Hub-Signature-256` signature validation**. It is meant to sit in front of the other workflows in this repository (e.g. [`whatsapp-inbound-support-router`](whatsapp-inbound-support-router.md), [`whatsapp-delivery-status-parser`](whatsapp-delivery-status-parser.md)) so that *those* workflows only ever receive requests that have already been cryptographically verified as genuinely coming from Meta.

- **GET requests** (Meta's subscription verification): checks `hub.mode` is `subscribe` and securely compares `hub.verify_token` against your configured secret. Responds with the exact `hub.challenge` value as plain text only on success; otherwise returns `403 Forbidden`. **Fails closed by design**: until you replace the shipped placeholder commitment with your own (see [Setup steps](#setup-steps)), every GET request is rejected, regardless of the token presented.
- **POST requests** (actual webhook deliveries): reads the raw request body exactly as sent, computes an HMAC-SHA256 over those exact bytes using your Meta app secret, and compares it — in constant time — against the `X-Hub-Signature-256` header Meta sent. Only a request with a valid signature reaches a clearly marked "verified" attachment point; everything else is rejected with `401 Unauthorized` before any further processing.

**Neither secret (verify token or app secret) ever appears as a plain value anywhere in this workflow's exported JSON.** Both live only in two separate n8n **Crypto** credentials, used as HMAC keys — see [Security design](#security-design) for exactly how, why this was verified experimentally rather than assumed, and what it does *not* protect against.

This workflow is a verified template, tested against a specific n8n version with a specific test suite documented below — it is **not** a certified production security product, and it does not by itself make an installation "secure." Read [Known limitations](#known-limitations) and [Infrastructure logging risk](#infrastructure-logging-risk) before deploying it.

## Real business use case

Every other workflow in this repository that receives inbound WhatsApp webhooks (delivery-status events, inbound messages) explicitly documents that it does **not** implement Meta's authenticity checks and must not be exposed directly to Meta. This workflow is that missing piece: a single, reusable gateway that verifies a request is genuinely from Meta before anything downstream (routing logic, database writes, business logic) ever sees it.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22).

## Required nodes

- **Webhook** (`n8n-nodes-base.webhook`, v2.1) — used twice, once for `GET` (verification) and once for `POST` (deliveries), both on the **same path** so Meta's single configured callback URL works for both.
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — two, documenting the GET and POST branches on-canvas.
- **Crypto** (`n8n-nodes-base.crypto`, v2) — used twice, in `Hmac` mode, each bound to its own **Crypto** credential (see below). This is the only n8n core node that can use a credential-stored secret as an HMAC key without that secret ever being readable from a workflow parameter or expression.
- **Code** (`n8n-nodes-base.code`, v2) — used twice, for the GET-verification decision and the POST-signature decision. The GET-verification Code node additionally validates that `EXPECTED_COMMITMENT` has been replaced with a properly-formatted value before accepting any request (see [Security design](#security-design)). Both contain only a hand-rolled constant-time hex-string comparison (see below for why `require('crypto')` was not used).
- **IF** (`n8n-nodes-base.if`, v2.3) — used twice, gating both branches before anything downstream runs.
- **NoOp** (`n8n-nodes-base.noOp`, v1) — one node, named `VERIFIED - Attach Downstream Processing Here`, marking exactly where a verified POST request's data becomes available for further processing.
- **Respond to Webhook** (`n8n-nodes-base.respondToWebhook`, v1.5) — four, one for each of GET-success / GET-rejected / POST-success / POST-rejected.

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature (confirmed: the execution-data settings used are Community-Edition-safe; the Enterprise-only `redactionPolicy` setting is deliberately not used — see [Execution-data persistence](#execution-data-persistence)).

## Security design

This design was **not assumed** — every mechanism below was verified experimentally against a live n8n v2.35.4 instance before being used. See [Test procedure](#test-procedure) for exactly how.

### Secret storage: the `crypto` credential type

n8n's core **Crypto** node declares a required credential of type `crypto`, which has an `Hmac Secret` field. When the Crypto node's action is `Hmac`, it reads this field internally (via `this.getCredentials('crypto')`, a mechanism only available to the node's own compiled implementation) and uses it as the HMAC key — the secret is never exposed to node parameters, expressions, or the Code node. This repository uses **two separate `crypto` credentials**: one holding your Meta **app secret** (for POST signature validation), one holding your **verify token** (for the GET handshake). Neither secret is present anywhere in `whatsapp-webhook-security-gateway.json` — confirmed by grepping the exported file.

**Important finding: n8n's Code node cannot access any credential, of any type, under any circumstance.** Its node-type definition declares no `credentials` field at all (unlike Webhook, Crypto, HTTP Request, and Respond to Webhook, which all do). This was verified directly, not assumed — it rules out the simplest possible design ("just compare the token in a Code node") and shaped everything below.

### GET verification without ever reading the verify token

Because a Code node cannot read the verify-token credential, and the Crypto node can only use it as an HMAC *key* (not read it as plain data), a direct `incoming === secret` comparison is not achievable with core nodes. Instead, this workflow uses an HMAC **commitment** scheme:

1. **At setup time** (once, outside n8n — use `scripts/generate_webhook_verify_commitment.py`, see [Setup steps](#setup-steps)), you compute `EXPECTED_COMMITMENT = HMAC-SHA256(key = your verify token, message = your verify token)`.
2. **At request time**, the `Hash GET Verify Token` Crypto node computes `HMAC-SHA256(key = verify token from credential, message = incoming hub.verify_token)`.
3. If the incoming token equals the real verify token, step 2's message equals step 1's message (both are the real token), so step 2's result equals `EXPECTED_COMMITMENT` exactly — proving a match without the Code node ever reading the secret. If the incoming token is wrong, the two HMACs differ (cryptographically, for all practical purposes).

**What this commitment does and does not protect.** `EXPECTED_COMMITMENT` is a one-way function of your verify token: it cannot be directly inverted to recover the token. It is **not**, however, a proof that the token itself is safe to treat as public, and it is **not** immune to offline guessing. Because the commitment is a *deterministic* function of the token, anyone who obtains it can test candidate tokens against it entirely offline (compute `HMAC-SHA256(candidate, candidate)` and compare) — with no rate limiting, no network round-trip, and no way for you to detect the attempt. A short, guessable, or dictionary-word verify token can be recovered this way even though the commitment "cannot be reversed" in the direct-inversion sense. This is why the setup script generates a **cryptographically random 256-bit token by default** and warns if you supply a shorter one with `--existing`: with sufficient entropy, offline guessing is computationally infeasible; without it, the commitment provides materially weaker protection than "safe to publish anywhere" would suggest. Treat `EXPECTED_COMMITMENT` as something you would still rather not disclose unnecessarily, not as public data.

This was verified to work correctly — see [Test procedure](#test-procedure) — with the correct token producing a match and any incorrect token producing a mismatch, and the fail-closed placeholder behavior was verified to reject every request, including one presenting the *correct* token, until a validly-formatted commitment is configured.

### POST signature validation over exact raw bytes

The `POST Trigger` Webhook node has its **Raw Body** option enabled. This was verified experimentally to preserve the exact request bytes (confirmed byte-for-byte, including deliberately unusual whitespace) in a binary property, available *alongside* n8n's normal parsed-JSON body — so this workflow never signs a re-serialized/re-parsed reconstruction of the payload, which would silently break on any whitespace difference from what Meta actually sent. The `Compute POST Body HMAC` Crypto node hashes that binary data directly (`Hmac`, `Binary File` enabled), using the app-secret credential as the key. The result was verified to exactly match an independently-computed HMAC-SHA256 (Python's `hmac`/`hashlib`) of the same bytes and key.

### Constant-time comparison without `require('crypto')`

`require('crypto')` **is blocked by default** in the Code node sandbox (confirmed experimentally: `Module 'crypto' is disallowed`) — enabling it requires the instance-wide `NODE_FUNCTION_ALLOW_BUILTIN` environment variable, which this project deliberately avoids for the same reason the [WhatsApp template message sender](whatsapp-template-message-sender.md) avoids `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`: it shouldn't require weakening a shared instance-wide security default. Instead, both comparisons use a small hand-written constant-time hex-string comparison (XOR-accumulate over every character, no early exit) — equivalent in effect to `crypto.timingSafeEqual` for same-length hex digests, without needing the blocked module.

### Structural rejection

Both branches use an **IF** node between the security check and anything else. This was verified directly: for every rejected test case (wrong/missing token, wrong mode, wrong/missing/malformed/truncated signature, tampered body, unconfigured placeholder), the `VERIFIED - Attach Downstream Processing Here` node **never executed** — confirmed by inspecting each execution's recorded node history, not just the HTTP response.

### The shipped placeholder fails closed

The `Evaluate GET Verification` Code node ships with `EXPECTED_COMMITMENT` set to the literal string `REPLACE_WITH_YOUR_COMMITMENT__SEE_SETUP_STEP_3` — a value that is deliberately **not** a plausible 64-character hex digest, so it cannot be mistaken for real setup material at a glance. The code validates the constant's format (`^[0-9a-f]{64}$`) before doing anything else; if it doesn't match, GET verification is forced to fail for every request, unconditionally — including a request presenting your actual, correct verify token. This was verified experimentally (see [Test procedure](#test-procedure)): with real, correctly-bound credentials but the placeholder still in place, a live GET request carrying the genuinely correct token was still rejected with `403`.

### Execution-data persistence

By default, n8n's execution history retains a full record of every workflow run, including trigger inputs — for the GET branch, that includes the incoming `hub.verify_token`; for the POST branch, the request body and `X-Hub-Signature-256` header. This workflow's `settings` block explicitly disables that retention:

```json
{
  "executionOrder": "v1",
  "saveDataErrorExecution": "none",
  "saveDataSuccessExecution": "none",
  "saveManualExecutions": false,
  "saveExecutionProgress": false
}
```

These are the standard n8n v2.35.4 workflow-level execution-saving settings (not an Enterprise feature, not an instance-wide environment variable) — every value here was applied through n8n itself (via the API, equivalent to the Workflow Settings panel in the editor), then round-tripped through n8n's official `export:workflow` / `import:workflow` CLI to confirm the exact property names and values survive unchanged. `redactionPolicy` (a related but Enterprise-licensed setting) is deliberately not used.

**What this achieves, verified experimentally, and what it does not:**

- With these settings, a webhook-triggered execution is immediately excluded from n8n's execution list, its single-execution detail API, and the editor's execution view — confirmed with unique per-test canary values (a distinct verify token, challenge string, and POST body marker) that could not be retrieved through any of those interfaces after the corresponding request completed.
- Directly inspecting the underlying SQLite database (not just the API) showed the same canary values were **still physically present** in the `execution_data` table immediately after the request — the execution row is soft-deleted (marked with a backdated `deletedAt`), not instantly erased. This is n8n's own standard pruning design (`execution-persistence.js` → `deleteInFlightExecution`), not a bug or a gap specific to this workflow.
- A background pruning job (`ExecutionsPruningService`), which every n8n installation runs by default, later physically removes rows marked this way. Its default interval is bounded (15 minutes) with a further deletion buffer (1 hour) — so under n8n's **default** configuration, physical removal from disk is **not instantaneous**, and could take up to roughly the sum of those two windows in the worst case. Under a test configuration with the pruning interval and buffer both minimized, physical removal (confirmed absent from the SQL-queryable execution data, and, after a WAL checkpoint, from a full raw-byte scan of the database file) completed within about a minute of the request, and the effect was re-confirmed to hold after restarting the n8n process and searching the database again.
- **Do not read this as "n8n never persists the secret" or "no secret ever touches disk."** The accurate claim is: these settings make the data unreachable through any n8n API, UI, or execution-history mechanism immediately, and guarantee it is queued for physical deletion by n8n's own standard background process on a bounded (not instant) schedule identical to what every other n8n workflow with these settings relies on. If your threat model requires guaranteed-instant physical erasure, these settings do not provide that, and no n8n workflow-level setting can.
- The app secret itself was never found in execution data in any test, under either the old or new settings — it is only ever used as an HMAC key by the Crypto node, never handled as data that could be recorded.

## Infrastructure logging risk

**This workflow's settings control what n8n itself retains. They cannot control anything outside n8n.** Meta's GET verification handshake places `hub.verify_token` directly in the callback URL's **query string**. Most infrastructure between the internet and your n8n instance — reverse proxies (nginx, Caddy, Traefik), load balancers, CDNs, API gateways, and APM/tracing/error-reporting tools (e.g. Datadog, Sentry, New Relic) — logs the full request URL, including the query string, by default. If any of that infrastructure is in front of your n8n instance, your verify token can end up in those logs regardless of anything this workflow or n8n's own execution settings do.

Before deploying this workflow with a real Meta subscription, you are responsible for:

- **Serving n8n over HTTPS only.** Meta requires this for webhook callback URLs, and it also means query-string exposure is limited to endpoints that terminate or log the request — not the open network.
- **Checking whether your reverse proxy, load balancer, or CDN logs full request URLs (including query strings) by default**, and disabling or redacting that logging for your webhook path specifically if so. The exact configuration is specific to your infrastructure (e.g. nginx's `log_format`, an ALB access-log field, a CDN's URL-logging toggle) and is outside what this workflow or its documentation can verify on your behalf.
- **Checking your APM, tracing, and error-reporting tool configuration** for the same thing — many of these capture full request URLs (and sometimes headers/bodies) as part of normal request tracing, independent of both n8n and your reverse proxy.
- **Restricting who can view n8n's own execution data and logs** (owner/admin-level access only), since even with persistence disabled at the workflow level, the brief pre-pruning window described above means the data is not instantaneously unreadable to someone with direct database or admin-panel access.
- **Setting an explicit retention and deletion policy** for any logs (proxy, CDN, APM) that do capture the query string, and knowing how to purge them if needed.
- **Rotating your verify token** (generate a new one with `scripts/generate_webhook_verify_commitment.py`, update the credential, recompute and redeploy `EXPECTED_COMMITMENT`, and update Meta's App Dashboard) if you suspect it was exposed through any of the above.

This is not a claim of universal n8n Cloud safety, nor a claim that this list is exhaustive for every possible deployment topology — it is the set of controls this project identified and could describe accurately without being able to verify n8n Cloud's or your own infrastructure's specific logging behavior. Verify your own stack before relying on it.

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change of any kind is required to run this workflow's own logic — the execution-persistence controls described in [Execution-data persistence](#execution-data-persistence) are workflow-level `settings`, not environment variables or instance configuration. This does **not** mean the workflow is safe to expose without further setup: see [Infrastructure logging risk](#infrastructure-logging-risk) for what remains your responsibility outside n8n itself.

## Required credentials

**Two**, both of n8n credential type **Crypto** (`crypto`):

| Credential | Bound to node | `Hmac Secret` field holds |
|---|---|---|
| e.g. "WhatsApp Gateway App Secret" | `Compute POST Body HMAC` | Your Meta app secret (from your Meta App's Basic Settings) |
| e.g. "WhatsApp Gateway Verify Token" | `Hash GET Verify Token` | Your chosen verify token — a cryptographically random value with at least 256 bits of entropy is required (not just recommended) for the commitment scheme's guarantees to hold; see [Security design](#security-design). Generate one with `scripts/generate_webhook_verify_commitment.py`. |

**Neither credential is included in the exported JSON.** Both Crypto nodes have no credential bound after import, by design — you must create both credentials yourself and select them on the respective nodes.

## Setup steps

1. Import `whatsapp-webhook-security-gateway.json` into your n8n instance.
2. Run the setup helper to generate a verify token and its commitment, without ever putting the token on your command line, in a file, or in shell history:
   ```
   python3 scripts/generate_webhook_verify_commitment.py
   ```
   This prints a newly generated, cryptographically random 256-bit verify token once, and the corresponding commitment value. **Copy the token immediately** into your password manager — it is not written to any file and will not be shown again by the script. (If you already have a token you want to keep using instead, run `python3 scripts/generate_webhook_verify_commitment.py --existing`, which prompts for it with hidden input — but note the script will warn you if it's shorter than the recommended 256 bits, since that weakens the commitment's offline-guessing resistance.)
3. Create your two **Crypto** credentials (see [Required credentials](#required-credentials)): your real Meta app secret, and the verify token from step 2. Bind them to the `Compute POST Body HMAC` and `Hash GET Verify Token` nodes respectively.
4. Open the `Evaluate GET Verification` Code node and replace the placeholder `EXPECTED_COMMITMENT` constant (`REPLACE_WITH_YOUR_COMMITMENT__SEE_SETUP_STEP_3`) with the commitment value printed in step 2. Until you do this, every GET verification request is rejected by design (see [Security design](#security-design)).
5. Activate the workflow. **If the workflow was already active when you edited the Code node in step 4, deactivate and reactivate it** (toggle the Active switch off, then on) before testing. This project observed, via the n8n REST API, that an already-active workflow's running webhook trigger did not pick up a Code node change until the workflow was deactivated and reactivated; this was not observed to be an issue when editing and saving through the n8n editor UI in the normal way, but reactivating is a cheap way to rule it out either way.
6. Note the generated webhook URL (path: `whatsapp-webhook-gateway`) — the **same URL** handles both Meta's GET verification and POST deliveries.
7. Configure this URL as your WhatsApp Cloud API webhook callback URL in Meta's App Dashboard, along with your verify token (must match what you put in the credential and used to compute the commitment). Read [Infrastructure logging risk](#infrastructure-logging-risk) before doing this against a real Meta subscription.
8. Attach your downstream processing (routing, parsing, persistence) after the `VERIFIED - Attach Downstream Processing Here` NoOp node — **never** add the customer payload to the `Respond POST Success` node's response body.
9. Test with a synthetic client first — see [Test procedure](#test-procedure) — before pointing a real Meta subscription at it.

## Test procedure

Built and verified in an isolated local n8n test environment (no Docker available; used the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2, with an isolated `N8N_USER_FOLDER` and SQLite database — no existing n8n instance, VPS, or credentials were touched). The real Meta API was never contacted — all testing used a synthetic HTTP client (`curl`) sending crafted requests with independently-computed signatures/commitments and unique, traceable synthetic values (labeled `CANARY_...`) so their presence or absence in execution history and the raw database could be checked unambiguously rather than inferred.

**Feasibility investigation (before building anything):** confirmed experimentally, in this order:
1. The Code node has no `credentials` field in its node-type definition — it cannot read any credential.
2. The Crypto node's `Hmac` action requires a `crypto`-type credential and uses its `hmacSecret` field internally as the HMAC key, never exposed to parameters/expressions/output.
3. The Webhook node's **Raw Body** option preserves the exact request bytes (verified byte-for-byte against the original, including deliberately irregular whitespace).
4. The Crypto node's `Hmac` action, with **Binary File** enabled, can hash that raw binary data directly, and its output was confirmed to exactly match an independently-computed HMAC-SHA256 (Python `hmac`/`hashlib`) of the same bytes and key.
5. `require('crypto')` is blocked by default in the Code node sandbox (`Module 'crypto' is disallowed`) — ruling out `crypto.timingSafeEqual` without an instance-wide setting change.
6. The commitment scheme described in [Security design](#security-design) was built and confirmed to correctly distinguish a matching token from a non-matching one.

**Building and testing the gateway itself:**

| # | Test | Result |
|---|---|---|
| 1 | Correct GET token and mode | `200`, body is the exact `hub.challenge` value |
| 2 | Wrong GET token | `403 Forbidden` |
| 3 | Missing GET token | `403 Forbidden` |
| 4 | Invalid mode | `403 Forbidden` |
| 5 | Correct GET token, but `EXPECTED_COMMITMENT` still the shipped placeholder | `403 Forbidden` — fails closed even with a genuinely correct token |
| 6 | Malformed/short `EXPECTED_COMMITMENT` values (not 64 lowercase hex characters) | Confirmed structurally: verification is forced false unconditionally by the format check, before any comparison runs |
| 7 | Valid POST signature | Reaches verified branch, `200 {"status":"received"}` |
| 8 | One-byte body modification (signature computed for the original) | `401 Forbidden` |
| 9 | JSON whitespace modification without recalculating signature | `401 Forbidden` |
| 10 | Wrong secret | `401 Forbidden` |
| 11 | Missing signature | `401 Forbidden` |
| 12 | Invalid prefix (`sha1=` instead of `sha256=`) | `401 Forbidden` |
| 13 | Truncated signature | `401 Forbidden` (no crash) |
| 14 | Upper/lowercase hex signature | `200`, verified (comparison lowercases the incoming hex first) |
| 15 | UTF-8 body (multi-byte characters) signed as exact bytes | `200`, verified |
| 16 | Malformed POST body (invalid JSON) | Rejected before reaching the workflow at all (n8n's own request body parser returns `422` — this is platform behavior upstream of the workflow, not something the workflow's logic controls) |
| 17 | Rejected requests never reach downstream | Confirmed for every rejected case above by inspecting each execution's recorded node history — the `VERIFIED -...` node never ran |
| 18 | Successful GET token absent from persisted execution data | Confirmed absent via the executions API/UI immediately; confirmed *physically present but soft-deleted* in SQLite immediately after, then confirmed physically absent (SQL-queryable and, after a WAL checkpoint, raw-byte-level) once n8n's background pruning ran — see [Execution-data persistence](#execution-data-persistence) |
| 19 | Rejected GET token absent from persisted execution data | Same result as #18, for a deliberately wrong token |
| 20 | Valid POST body and signature absent from persisted execution data | Same result as #18, for a canary POST body and its signature header |
| 21 | Rejected POST body and signature absent from persisted execution data | Same result as #18, for a rejected POST attempt |
| 22 | Settings preserved after clean export/import | The repository's exact `whatsapp-webhook-security-gateway.json` was imported via the official `n8n import:workflow` CLI into a clean instance and re-exported via `n8n export:workflow`; the `settings` block and the `Evaluate GET Verification` Code node's content were byte-for-byte identical before and after |
| 23 | Clean import, credential rebinding, full repetition | Tests 1–21 re-run against a second instance after exporting via the official CLI, sanitizing, importing, and creating + binding two **new** synthetic credentials there — every result was identical |
| 24 | Setup script never accepts a token via the command line | Confirmed by automated unit test (`scripts/test_generate_webhook_verify_commitment.py`) that the script's argument parser rejects any positional argument and its source contains no code path that reads a token from `sys.argv` |
| 25 | Setup script never writes to disk | Confirmed by automated unit test asserting the script's source contains no file-write-shaped calls (`open(`, `Path(`, `.write(`, etc.) |
| 26 | Setup script warns on a short/weak `--existing` token | Confirmed by automated unit test |

Exported the workflow via n8n's official CLI (`n8n export:workflow`), stripped instance-specific metadata and both credential references entirely, imported the sanitized `.json` into a clean instance via `n8n import:workflow`, created new synthetic credentials there, manually bound them, and re-ran the applicable tests with identical results. Removed all temporary instances, credentials, and processes after evidence was collected. A repository-wide scan for every canary value used in this round of testing found none committed to any file.

All test data was synthetic: fake app secret and verify token values clearly labeled `CANARY_...`, fake challenge strings, and fabricated request bodies — no real Meta credentials or customer data anywhere, and none of these synthetic values are present in the committed workflow or documentation.

## Known limitations

- **No replay or deduplication protection.** A validly-signed request replayed later (e.g. by an attacker who captured it, or by Meta's own retry behavior) will pass verification again. If your downstream processing isn't naturally idempotent, add your own deduplication (e.g. tracking `providerMessageId`/`entry.id` values you've already processed).
- **Downstream persistence is not implemented here.** This gateway only verifies and marks a request as safe to process — it does not store, forward, or act on anything itself.
- **The commitment scheme's guarantees depend on your verify token's entropy.** A short or guessable token can be recovered from its published commitment via offline brute force even though the commitment cannot be directly reversed — see [Security design](#security-design). Use `scripts/generate_webhook_verify_commitment.py` without `--existing` to get a properly random one.
- **A compromised app secret or verify token defeats this entirely** — this gateway assumes both are kept genuinely secret; it has no additional layer of protection.
- **Execution-data persistence is disabled by this workflow's settings, but physical deletion is asynchronous and bounded, not instantaneous** — see [Execution-data persistence](#execution-data-persistence) for exactly what was verified and what the timing bound is under n8n's defaults.
- **Meta's GET handshake places your verify token in the callback URL's query string, which is outside this workflow's or n8n's control** — your own reverse proxy, load balancer, CDN, or APM tooling may log it independently. See [Infrastructure logging risk](#infrastructure-logging-risk).
- **The shipped `EXPECTED_COMMITMENT` is an unmistakable placeholder, not real setup material** — until replaced, the workflow rejects all GET requests. This is intentional fail-closed behavior, not a bug to work around; see [Setup steps](#setup-steps).
- This workflow has been verified as a template against the specific n8n version, node versions, and test suite documented here. It is **not** a certified production security product, and passing this test suite is not a guarantee against every possible attack — review [Infrastructure logging risk](#infrastructure-logging-risk) and your own deployment before relying on it.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Data handled

Reads the raw GET query parameters and POST body/headers Meta sends. Computes HMAC digests using two credential-stored secrets, but never returns either secret in any response. The GET success response returns only the `hub.challenge` value Meta itself supplied in that same request. The POST success response returns only `{"status":"received"}` — never the customer payload. Rejected requests receive only `"Forbidden"` with an appropriate status code. Nothing is written to a database or sent onward by this workflow itself. Execution-data persistence is disabled by this workflow's settings for successful, failed, and manual runs — see [Execution-data persistence](#execution-data-persistence) for what that does and does not guarantee.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-19
