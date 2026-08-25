# Customer Quotation Delivery

## What it does

A reusable **sub-workflow** (triggered via an Execute Workflow Trigger, meant to be called from another n8n workflow — not a standalone webhook) that delivers a single, **already-created and business-approved** quotation to its customer over WhatsApp, and durably records the outcome in Postgres. It does not create quotations, does not calculate prices, tax, or totals, and does not generate a PDF or any legally authoritative quotation document — see [Exact scope](#exact-scope) for exactly what "delivery" means here.

It accepts `idempotencyKey` (a durable idempotency key), `quotationId`, and `expectedVersion` (optimistic concurrency) — nothing else. **`recipientPhone`, the quotation number, amount, currency, valid-until date, and the secure quotation link are not caller inputs.** They are read exclusively from Postgres (`quotations`) — arbitrary caller-supplied data can never redirect a delivery to a different recipient, fabricate an amount, or forge a quotation link.

Every request is validated strictly, recorded exactly once even if the same `idempotencyKey` is retried or arrives concurrently, and never allowed to silently overwrite a newer quotation state.

**Postgres and the WhatsApp Business Cloud API cannot be joined into one atomic transaction.** A send failure *after* a successful, durable database reservation is reported honestly as `failed` — never as false success, and the reservation is never rolled back to "fix" it. If the database write that would record a *successful* send itself fails, the delivery is left in an explicit pending or reconciliation state rather than falsely reported as sent or automatically resent.

## Exact scope

**This workflow delivers a quotation that already exists, has already been reviewed, and has already been marked `approved` by whatever system owns real quotation data.** It does not:

- Create, price, or calculate a quotation, line items, subtotal, tax, discount, or total.
- Generate a PDF, an HTML document, or any other rendered quotation artifact.
- Decide *that* a quotation is worth sending, or *when* — that decision belongs entirely to whatever calls this workflow.
- Determine whether a quotation is legally binding, compliant, or correctly taxed in any jurisdiction — see [Regional and legal limitation](#regional-and-legal-limitation).

**"Sent" means the WhatsApp Business Cloud API accepted the message for delivery — not that it was delivered, read, or acted on.** This workflow's `sent` status reflects only a successful API response (see [Sender-output fail-closed design](#sender-output-fail-closed-design)); actual delivery/read receipts are a separate concern (see [`whatsapp-delivery-status-parser`](whatsapp-delivery-status-parser.md) for that layer, not called by this workflow).

The message itself is a concise summary — quotation number, formatted total, currency, valid-until date, and a secure link — sent through an approved WhatsApp template. The quotation's actual content (line items, terms) lives at the linked page, which this workflow never fetches, renders, or inspects; it only passes a validated link through to the template.

## Real business use case

An approved quotation sitting in the business's own system is worthless until the customer actually sees it. This workflow is the "deliver this specific, already-approved quotation exactly once, safely" step — whatever decides *that* a quotation should go out now (a sales action, an automated approval-triggered job) calls this workflow once per candidate delivery, passing only an identity/concurrency triple; this workflow is what actually, safely, sends anything.

## Required n8n version

Built and tested against **n8n v2.35.4**, running on Node.js v22.23.2 (n8n 2.35.4 requires Node.js ≥ 22.22), against an isolated **PostgreSQL 16.15** instance for testing.

## Required nodes

- **Execute Workflow Trigger** (`n8n-nodes-base.executeWorkflowTrigger`, v1.2) — entry point; declares the three-field input contract (no recipient, quotation values, or link).
- **Sticky Note** (`n8n-nodes-base.stickyNote`, v1) — in-canvas scope notes; not part of execution.
- **Code** (`n8n-nodes-base.code`, v2) — used ten times: input validation, deployment-configuration loading and validation (`Load Configuration` — the single authoritative source for hardcoded WhatsApp/link-origin settings, fails closed before any Postgres call), the configuration-rejected response builder, reserve-result classification, the status-response builder, the send-request builder (link/money validation and formatting), the invalid-send-request-result builder (reads the ownership-verified Postgres cleanup this path performs, see below), send-result classification, and the final finalize-response builder — plus the rejected-input response builder.
- **IF** (`n8n-nodes-base.if`, v2.3) — used four times: input validity, deployment-configuration validity, whether a send is actually needed, and whether the built send request (link token, formatted amount) itself passed validation before ever reaching the sender.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used three times (`Reserve And Apply`, `Finalize Invalid Send Request`, `Finalize Send Result`), `Execute Query` operation, every query fully parameterized (`$1, $2, ...` placeholders with a separate values array — never string-built SQL). See [Atomic idempotency and ownership design](#atomic-idempotency-and-ownership-design).
- **Execute Workflow** (`n8n-nodes-base.executeWorkflow`, v1.2) — the single call to the existing [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) sub-workflow, referenced by its stable, committed workflow id (`R1QDUW9jYqxREyDS`) — the same portable-by-id pattern already established by [`whatsapp-appointment-reminder`](whatsapp-appointment-reminder.md) and [`unpaid-invoice-reminder`](unpaid-invoice-reminder.md). See [Sender sub-workflow binding](#sender-sub-workflow-binding).

All node types are part of n8n core — no community nodes required, and nothing here requires an n8n Enterprise-licensed feature.

## Feasibility investigation

Every mechanism below was verified experimentally against a live n8n v2.35.4 instance and an isolated local PostgreSQL 16.15 instance before being used.

- **The existing sender cannot send media, documents, or attachments** — its committed input contract (`recipientPhone`, `templateName`, `languageCode`, `graphApiVersion`, `phoneNumberId`, optional `bodyParameters`) has no field for a document/media payload, confirmed by direct inspection of its committed JSON and documentation. Extending it to do so would mean modifying the already-committed, already-tested sender or adding an undocumented external dependency — out of scope for this correction. **This workflow does not claim or fabricate PDF/document-delivery capability**; it stays link-based, exactly as the task that produced it required if that capability wasn't safely available.
- **A hardcoded-origin-plus-opaque-token link (Option A) is both safer and simpler than a host-allowlist (Option B).** With Option A, `quotations.link_token` is never a URL at all — it is a bounded, strictly-charactered opaque string (`^[A-Za-z0-9_-]{16,128}$`), and this workflow always builds the final link itself as `<fixed HTTPS origin>` + the token. There is no scheme, host, userinfo, port, or path-traversal sequence a compromised or malformed database row could ever redirect, because none of those are ever read from Postgres in the first place — Option B would still require validating an entire URL's structure at runtime. Verified directly: even after temporarily dropping the database's own `CHECK` constraint on `link_token` and inserting a URL-shaped value (`http://evil.example.com/steal?x=1`), this workflow's own n8n-layer validation (in `Build Send Request`) still rejected it before ever reaching the sender — zero mock calls, `invalid_database_state` reported.
- **Postgres `BIGINT` values arrive at a Code node as a JavaScript string, not a number** — a real defect found during this build (not merely anticipated): the initial money-formatting logic assumed `total_minor_units` would be a JS number and silently failed (`invalid_database_state`, zero sends) on every genuinely eligible quotation. Fixed to accept either a safe-integer number or a bounded digit-string, formatting via pure string slicing in both cases — never a floating-point division. See [Money representation](#money-representation).
- **The atomic reservation/finalization pattern is the same exception-safe ownership gate already proven (and, in one prior workflow, corrected after a real ownership-verification defect) elsewhere in this repository** — see [Atomic idempotency and ownership design](#atomic-idempotency-and-ownership-design) for the exact mechanism and what was verified.
- **Feasibility verdict: GO**, link-based scope. All of the above were verified experimentally; no fabricated workflow content was used.

## Input and trust boundary

The caller supplies exactly three fields: `idempotencyKey`, `quotationId`, `expectedVersion`. Every other value this workflow uses is either read from Postgres or is trusted, hardcoded configuration:

| Value | Source | Why |
|---|---|---|
| `recipientPhone`, quotation number, `total_minor_units`, `currency`, `valid_until`, `link_token`, `status` | `quotations` table, read back from the atomic reservation call | A caller that could set these could redirect a delivery to a different phone number, fabricate an amount, forge a quotation link, or claim a non-approved quotation is approved |
| `templateName`, `languageCode` | Hardcoded constants in `Build Send Request` | Trusted deployment configuration, not something a caller should choose arbitrarily |
| `graphApiVersion`, `phoneNumberId` | Hardcoded constants in `Build Send Request` | Deployment-level WhatsApp Business configuration — see [Setup steps](#setup-steps) for what to replace before real use |
| The quotation link's HTTPS origin | Hardcoded constant in `Build Send Request` (`QUOTATION_LINK_ORIGIN`) | Only the opaque token varies per quotation; the origin itself is never caller- or database-supplied — see [Link security design](#link-security-design) |
| "now" (for the expiry check) | Postgres's own `now()`, evaluated inside `process_quotation_delivery` | No caller-controlled clock — a caller cannot claim a quotation is still valid by supplying a fabricated timestamp |

An extra, unexpected field in the trigger input (e.g. a caller attempting to also pass `linkToken` or `recipientPhone`) has no effect — `Validate Input` only ever reads the three declared fields by name; anything else in the input object is simply never referenced anywhere in this workflow.

**Deployment configuration (WhatsApp/Graph API settings, the quotation-link origin) is validated by `Load Configuration` before any Postgres call is ever made.** This is a distinct, earlier gate than the input-trust-boundary table above — see [Link security design](#link-security-design) for why, and [Known issue found and fixed during this build](#known-issue-found-and-fixed-during-this-build) for the defect this closes.

## Link security design

**Option A was chosen: a fixed, hardcoded HTTPS origin plus a strictly validated opaque token — never a full URL read from Postgres or the caller.**

```js
const QUOTATION_LINK_ORIGIN = 'https://quotes.example.com/q/'; // replace before real use -- see Setup steps

function isValidLinkToken(v) {
  return typeof v === 'string' && v.length >= 16 && v.length <= 128 && /^[A-Za-z0-9_-]+$/.test(v);
}

const quotationLink = QUOTATION_LINK_ORIGIN + encodeURIComponent(c.linkToken);
```

`link_token` is bounded to a strict alphanumeric-plus-underscore-plus-hyphen character set at the database layer (`quotations_link_token_check`, plus a `UNIQUE` constraint — no two quotations can ever share a token) and re-validated identically in `Build Send Request` before ever being used to build a URL — defense in depth, not reliance on the database constraint alone. This character set structurally **cannot** contain a URL scheme (`http://`), a different host, userinfo (`user:pass@`), a port, or a path-traversal sequence (`../`) — there is nothing to allowlist against because none of those characters are ever valid in the first place. Verified directly: a URL-shaped, userinfo-shaped, and path-traversal-shaped token were each rejected by the database `CHECK` constraint at `INSERT` time; a URL-shaped token that bypassed the constraint (dropped temporarily for this test) was still rejected by the n8n-layer validation, with zero calls reaching the sender.

**Every hardcoded deployment constant is validated for format, not just the two with an obvious placeholder value**, by a separate `Load Configuration` node that runs before any Postgres call. `Load Configuration` is the single authoritative source for `GRAPH_API_VERSION`, `PHONE_NUMBER_ID`, `TEMPLATE_NAME`, `LANGUAGE_CODE`, and `QUOTATION_LINK_ORIGIN` — `Build Send Request` reads all of them from `Load Configuration`'s already-validated output rather than re-declaring its own copies, so the two can never diverge. `GRAPH_API_VERSION`/`PHONE_NUMBER_ID`/`TEMPLATE_NAME`/`LANGUAGE_CODE` are each checked against the same format the sender's own `Validate & Build Request` node independently enforces (so a value accepted here can never be one the sender itself would then reject). `PHONE_NUMBER_ID` additionally rejects **any all-zero value, regardless of length** — `/^0+$/`, a general rule rather than an exact-match placeholder set — so `'00000'` and `'00000000000000000000'` are rejected exactly the same way the shipped 15-zero placeholder is, not just that one specific string.

**The origin (`QUOTATION_LINK_ORIGIN`) gets the most scrutiny**, since it's syntactically valid HTTPS by construction and a forgotten placeholder would otherwise reach a real WhatsApp send carrying a broken link:

```js
function isValidOrigin(origin) {
  if (typeof origin !== 'string') return false;
  if (origin.indexOf('\\') !== -1) return false;
  const PREFIX = 'https://';
  if (origin.slice(0, PREFIX.length) !== PREFIX) return false;
  const rest = origin.slice(PREFIX.length);
  const slashIdx = rest.indexOf('/');
  if (slashIdx === -1) return false;
  const hostPart = rest.slice(0, slashIdx);
  const pathPart = rest.slice(slashIdx);
  if (hostPart.indexOf('@') !== -1) return false;
  if (hostPart.indexOf(':') !== -1) return false;
  if (hostPart.indexOf('?') !== -1 || hostPart.indexOf('#') !== -1) return false;
  if (!isValidHostname(hostPart)) return false;
  const host = hostPart.toLowerCase();
  if (host === 'localhost') return false;
  if (isReservedExampleHost(host)) return false;
  if (pathPart.charAt(pathPart.length - 1) !== '/') return false;
  if (pathPart.indexOf('?') !== -1 || pathPart.indexOf('#') !== -1) return false;
  if (!/^[A-Za-z0-9._/-]*$/.test(pathPart)) return false;
  const segments = pathPart.split('/').filter((s) => s.length > 0);
  for (const seg of segments) {
    if (!isValidPathSegment(seg)) return false;
  }
  return true;
}
```

Fixed HTTPS scheme, no username/password/port/query/fragment/backslash, always ending in `/` so the token can be safely appended as its own path segment. Hostname validity (`isValidHostname`) is checked with plain string splitting and per-label checks — **deliberately not one large nested regex**: each label between dots must be 1–63 characters, letters/digits/hyphen only, never starting or ending with a hyphen; the hostname must have at least two labels and be ASCII, 1–253 characters total; **every label being purely numeric is rejected outright** (`isNumericOnlyHost`), regardless of label count — this catches the canonical dotted-quad (`127.0.0.1`), a shortened/integer-like form (`127.1`), and an out-of-range form (`999.999.999.999`) all with the same rule, not a four-label-only check; and **the final label must contain at least one ASCII letter** (`hasLetter`) as a second, independent line of defense — a real TLD is never purely numeric. Together these still correctly allow a genuine numeric *subdomain* of an otherwise ordinary hostname, since the check is about the whole host being numeric, not any individual label — `123.quotes.company.com` is accepted. `isReservedExampleHost` rejects `example.com`/`example.org`/`example.net` **and every genuine subdomain of them** (a dot-bounded suffix match, not a naive `endsWith` — `quotesexample.net` is *not* a subdomain of `example.net` and is correctly allowed through). The path is validated segment-by-segment (`isValidPathSegment`), not by one regex over the whole path — a segment equal to `.` or `..` is rejected outright, so a client or intermediary that normalizes `/../` in the path can never redirect the configured quotation base path somewhere else.

**This is deliberately implemented with plain string operations, not the `URL` global** — a real portability defect found and fixed live during an earlier round of this correction: a draft origin validator used `new URL(origin)`, which threw `ReferenceError: URL is not defined` inside n8n's own Code node execution sandbox (confirmed by direct execution — `typeof URL` is `'undefined'` there, even though `URL` is an ordinary global in a plain Node.js process). Every placeholder-configuration test in [Test procedure](#test-procedure) is run against the real graph, not just the isolated function, specifically because this class of defect is invisible to logic review alone.

A shipped placeholder or malformed value in any field routes to `Build Configuration Rejected Response` — `configuration_required`, **zero Postgres calls, zero sender calls**, verified directly against the real committed workflow with its shipped defaults untouched. Verified with unit cases across all five fields (valid production-shaped values, every shipped placeholder, malformed Graph version/phone-number-id/template-name/language-code, all-zero phone-number ids of varying length, leading/trailing/consecutive-dot hosts, leading/trailing-hyphen labels, `example.com`/`.org`/`.net` and genuine subdomains of them, the `quotesexample.net` lookalike, numeric-subdomain hosts like `123.quotes.company.com`, `localhost`, IPv4/IPv6 literals including shortened and out-of-range numeric-only forms, userinfo/port/query/fragment/backslash-confusable origins, non-HTTPS, `.`/`..` path segments, and valid multi-label origins with single- and multi-segment paths) — run both in isolation and against the real n8n Code-node execution environment, since the `URL`-global defect above proved isolated Node.js testing alone is not sufficient — plus live against both the real committed workflow (shipped placeholder → `configuration_required`) and a mock-bound copy with a real, non-placeholder configuration (→ a genuine successful send).

**This workflow never fetches or follows the quotation link.** It only passes the validated, constructed link into the sender's `bodyParameters` — the same as any other template parameter — and the sender itself only ever sends it as literal template text to the WhatsApp Business Cloud API, never dereferencing it either.

The controlled output (`status`, `quotationId`, `httpStatus`, `providerMessageId`) never contains the link or the token — see [Controlled output contract](#controlled-output-contract).

## Money representation

**Integer minor units** (`total_minor_units BIGINT`, e.g. cents), not `NUMERIC`, because this workflow only *displays* a total already calculated by the quotation-owning application — it never performs arithmetic on it. A 2-decimal-place exponent is assumed for the displayed format (correct for EUR, USD, GBP, and most ISO 4217 currencies); this is a documented limitation for 0-decimal currencies (e.g. JPY) or 3-decimal currencies (e.g. BHD, KWD) — see [Known limitations](#known-limitations).

**Never JavaScript floating-point arithmetic.** Formatting is pure string manipulation:

```js
function formatMinorUnits(minorUnits) {
  // Postgres BIGINT columns are returned by the driver as a numeric
  // STRING, not a JS number, specifically to avoid silent precision loss
  // for values near/above Number.MAX_SAFE_INTEGER -- both shapes are
  // accepted here, but formatting itself is always pure string slicing,
  // never a numeric division, regardless of which shape arrived.
  let s;
  if (typeof minorUnits === 'number' && Number.isInteger(minorUnits) && Number.isSafeInteger(minorUnits) && minorUnits >= 0) {
    s = String(minorUnits);
  } else if (typeof minorUnits === 'string' && /^[0-9]{1,15}$/.test(minorUnits)) {
    s = minorUnits;
  } else {
    return null;
  }
  const padded = s.padStart(3, '0');
  const whole = padded.slice(0, -2);
  const frac = padded.slice(-2);
  return whole + '.' + frac;
}
```

**Why both a number and a string are accepted, and why this matters:** the initial implementation assumed `total_minor_units` would arrive as a JS number (as most Postgres integer types do through this driver) and only checked `Number.isInteger(...)`. It didn't — Postgres `BIGINT` is returned as a string specifically because some 64-bit values cannot be represented exactly as JS numbers, and n8n's Postgres node passes that through unchanged. Every genuinely eligible quotation was silently failing (`invalid_database_state`, zero sends) until this was caught by actually running the real send path against live Postgres, not by reasoning about the code alone. `total_minor_units` is `CHECK`-constrained to at most 12 digits (`999999999999`), always safely within both a JS safe integer and the string-regex bound, so no precision is ever actually at risk either way — only the *shape* needed handling.

If `formatMinorUnits` returns `null` (a database value the workflow's own validation doesn't trust, despite the database's own `CHECK` constraint), the request fails closed via `Send Request Valid?` — the sender is never called.

**Tax, discount calculation, and quotation-law compliance remain entirely the responsibility of the quotation-owning business/application, and may differ by jurisdiction.** This workflow does not recalculate, verify, or claim authority over any of it — see [Regional and legal limitation](#regional-and-legal-limitation).

## Atomic idempotency and ownership design

**Three tables**, kept separate from each other for the same reason the corrected `unpaid-invoice-reminder` design does: `quotations` is owned and written by the quotation-owning business application, never by this workflow, and keeping delivery-tracking columns out of it makes that guarantee structurally obvious.

```sql
CREATE TABLE quotations (
  quotation_id     TEXT PRIMARY KEY,
  quotation_number TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'approved' | 'cancelled' | 'rejected' | 'accepted'
  recipient_phone  TEXT NOT NULL,
  total_minor_units BIGINT NOT NULL,                -- integer minor units (e.g. cents) -- see Money representation
  currency         TEXT NOT NULL,                   -- 3-letter ISO code, 2-decimal exponent assumed
  valid_until      TIMESTAMPTZ NOT NULL,
  link_token       TEXT NOT NULL,                   -- opaque, bounded, safe-charset -- never a full URL
  version          INTEGER NOT NULL DEFAULT 1,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT quotations_quotation_id_check CHECK (quotation_id ~ '^[A-Za-z0-9_-]{1,128}$'),
  CONSTRAINT quotations_quotation_number_check CHECK (length(quotation_number) BETWEEN 1 AND 64 AND quotation_number ~ '^[A-Za-z0-9._-]+$'),
  CONSTRAINT quotations_status_check CHECK (status IN ('draft', 'approved', 'cancelled', 'rejected', 'accepted')),
  CONSTRAINT quotations_recipient_phone_check CHECK (recipient_phone ~ '^[1-9][0-9]{7,14}$'),
  CONSTRAINT quotations_total_minor_units_check CHECK (total_minor_units >= 0 AND total_minor_units <= 999999999999),
  CONSTRAINT quotations_currency_check CHECK (currency ~ '^[A-Z]{3}$'),
  CONSTRAINT quotations_link_token_check CHECK (length(link_token) BETWEEN 16 AND 128 AND link_token ~ '^[A-Za-z0-9_-]+$'),
  CONSTRAINT quotations_link_token_unique UNIQUE (link_token),
  CONSTRAINT quotations_version_check CHECK (version >= 1 AND version <= 1000000000),
  CONSTRAINT quotations_valid_until_finite_check CHECK (isfinite(valid_until))
);

CREATE TABLE quotation_delivery_state (
  quotation_id            TEXT PRIMARY KEY,
  status                  TEXT NOT NULL,   -- 'delivery_pending' | 'sent' | 'failed' | 'reconciliation_required'
  active_idempotency_key  TEXT,             -- the key currently owning this quotation's pending/terminal attempt
  reserved_version        INTEGER,          -- the quotation version observed when active_idempotency_key reserved
  provider_message_id     TEXT,
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT qds_quotation_id_check CHECK (quotation_id ~ '^[A-Za-z0-9_-]{1,128}$'),
  CONSTRAINT qds_status_check CHECK (status IN ('delivery_pending', 'sent', 'failed', 'reconciliation_required', 'invalid_database_state')),
  CONSTRAINT qds_active_key_check CHECK (active_idempotency_key IS NULL OR active_idempotency_key ~ '^[A-Za-z0-9_-]{1,128}$'),
  CONSTRAINT qds_reserved_version_check CHECK (reserved_version IS NULL OR (reserved_version >= 1 AND reserved_version <= 1000000000)),
  CONSTRAINT qds_provider_message_id_check CHECK (provider_message_id IS NULL OR (length(provider_message_id) BETWEEN 1 AND 256 AND provider_message_id ~ '^[A-Za-z0-9_.=-]+$')),
  CONSTRAINT qds_provider_message_id_status_check CHECK (
    (status = 'sent' AND provider_message_id IS NOT NULL) OR
    (status <> 'sent' AND provider_message_id IS NULL)
  )
);

CREATE TABLE quotation_delivery_events (
  idempotency_key  TEXT PRIMARY KEY,
  quotation_id     TEXT NOT NULL,
  reserved_version INTEGER,             -- persisted at reservation time -- the finalize concurrency guard, never caller input
  result_status    TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT qde_idempotency_key_check CHECK (idempotency_key ~ '^[A-Za-z0-9_-]{1,128}$'),
  CONSTRAINT qde_quotation_id_check CHECK (quotation_id ~ '^[A-Za-z0-9_-]{1,128}$'),
  CONSTRAINT qde_reserved_version_check CHECK (reserved_version IS NULL OR (reserved_version >= 1 AND reserved_version <= 1000000000)),
  CONSTRAINT qde_result_status_check CHECK (result_status IN (
    'reserved', 'missing_quotation', 'conflict', 'draft', 'cancelled', 'rejected',
    'accepted', 'expired', 'already_delivered', 'delivery_pending',
    'sent', 'failed', 'reconciliation_required', 'invalid_database_state'
  ))
);

CREATE OR REPLACE FUNCTION process_quotation_delivery(
  p_idempotency_key   TEXT,
  p_quotation_id      TEXT,
  p_expected_version  INTEGER
) RETURNS TABLE (
  route                  TEXT,   -- 'owner_applied' | 'duplicate_match' | 'idempotency_mismatch'
  result_status           TEXT,
  out_recipient_phone     TEXT,
  out_quotation_number    TEXT,
  out_total_minor_units   BIGINT,
  out_currency            TEXT,
  out_valid_until         TEXT,
  out_link_token          TEXT,
  out_new_version          INTEGER
) AS $$
DECLARE
  v_existing quotation_delivery_events%ROWTYPE;
  v_quote quotations%ROWTYPE;
  v_state quotation_delivery_state%ROWTYPE;
BEGIN
  BEGIN
    INSERT INTO quotation_delivery_events (idempotency_key, quotation_id, reserved_version, result_status)
    VALUES (p_idempotency_key, p_quotation_id, NULL, 'reserved');
  EXCEPTION WHEN unique_violation THEN
    SELECT * INTO v_existing FROM quotation_delivery_events WHERE idempotency_key = p_idempotency_key;
    IF v_existing.quotation_id = p_quotation_id THEN
      RETURN QUERY SELECT 'duplicate_match'::TEXT, v_existing.result_status, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::INTEGER;
    ELSE
      RETURN QUERY SELECT 'idempotency_mismatch'::TEXT, 'idempotency_mismatch'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::INTEGER;
    END IF;
    RETURN;
  END;

  SELECT * INTO v_quote FROM quotations WHERE quotation_id = p_quotation_id FOR UPDATE;

  IF NOT FOUND THEN
    UPDATE quotation_delivery_events SET result_status = 'missing_quotation' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'missing_quotation'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::INTEGER;
    RETURN;
  END IF;

  UPDATE quotation_delivery_events SET reserved_version = v_quote.version WHERE idempotency_key = p_idempotency_key;

  IF v_quote.version <> p_expected_version THEN
    UPDATE quotation_delivery_events SET result_status = 'conflict' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'conflict'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  IF v_quote.status <> 'approved' THEN
    -- Defense in depth: quotations_status_check already bounds v_quote.status
    -- to a known set under normal operation, but this function never trusts
    -- that as the only line of defense. Writing an unrecognized value
    -- straight into quotation_delivery_events.result_status would violate
    -- ITS OWN separate, narrower CHECK constraint and abort the whole
    -- statement with a hard error rather than a controlled response -- so
    -- the value is validated here first, and an unrecognized status
    -- degrades to the honest, explicit 'invalid_database_state' instead.
    IF v_quote.status IN ('draft', 'cancelled', 'rejected', 'accepted') THEN
      UPDATE quotation_delivery_events SET result_status = v_quote.status WHERE idempotency_key = p_idempotency_key;
      RETURN QUERY SELECT 'owner_applied'::TEXT, v_quote.status, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    ELSE
      UPDATE quotation_delivery_events SET result_status = 'invalid_database_state' WHERE idempotency_key = p_idempotency_key;
      RETURN QUERY SELECT 'owner_applied'::TEXT, 'invalid_database_state'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    END IF;
    RETURN;
  END IF;

  -- Database-layer defense in depth, checked before the expiry comparison
  -- itself: quotations_valid_until_finite_check already bounds valid_until
  -- to a finite instant under normal operation, but this function never
  -- trusts that as the only line of defense. PostgreSQL's timestamptz type
  -- accepts 'infinity'/'-infinity' as valid values -- 'infinity' is never
  -- <= now() and would otherwise pass the expiry check below and reach a
  -- real WhatsApp send as the literal string "infinity"; '-infinity' IS
  -- always <= now() and would otherwise be mislabeled 'expired' rather
  -- than honestly reported as corrupt data. Both directions are checked
  -- here, before the expiry comparison, so neither can slip through under
  -- either label.
  IF NOT isfinite(v_quote.valid_until) THEN
    UPDATE quotation_delivery_events SET result_status = 'invalid_database_state' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'invalid_database_state'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  -- Inclusive expiry boundary: a quotation whose valid_until is exactly
  -- "now" is treated as already expired, not as valid for one more instant.
  IF v_quote.valid_until <= now() THEN
    UPDATE quotation_delivery_events SET result_status = 'expired' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'expired'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  SELECT * INTO v_state FROM quotation_delivery_state WHERE quotation_id = p_quotation_id FOR UPDATE;

  IF FOUND AND v_state.status = 'sent' THEN
    UPDATE quotation_delivery_events SET result_status = 'already_delivered' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'already_delivered'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  IF FOUND AND v_state.status = 'delivery_pending' THEN
    UPDATE quotation_delivery_events SET result_status = 'delivery_pending' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'delivery_pending'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  IF FOUND AND v_state.status = 'reconciliation_required' THEN
    -- A quotation stuck in reconciliation_required requires manual or
    -- separate-workflow resolution -- it must NEVER be silently reopened
    -- and resent just because a new idempotencyKey showed up. This fresh
    -- key records its own non-owning event result but never touches
    -- quotation_delivery_state, and never reaches the sender.
    UPDATE quotation_delivery_events SET result_status = 'reconciliation_required' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'reconciliation_required'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  IF FOUND AND v_state.status = 'invalid_database_state' THEN
    -- A quotation left in invalid_database_state (the underlying quote
    -- data itself was invalid -- see the data-integrity check below, and
    -- the n8n-layer defense-in-depth cleanup this state also records)
    -- requires manual correction of the quotation's own data before any
    -- further delivery attempt -- it must NEVER be silently reopened.
    -- Distinct from reconciliation_required: zero sender calls ever
    -- happened here, so there is no "may have already reached WhatsApp"
    -- ambiguity -- the block exists purely so a human fixes the data
    -- first, not because the outcome is uncertain.
    UPDATE quotation_delivery_events SET result_status = 'invalid_database_state' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'invalid_database_state'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  -- v_state.status = 'failed' (or no row at all) intentionally falls
  -- through to a fresh reservation below: a known, definite send failure
  -- (a synchronous non-2xx rejection from WhatsApp, or an internally
  -- inconsistent malformed response) is safe to manually retry with a new
  -- idempotencyKey -- see Crash and reconciliation behavior. This is
  -- deliberately NOT the same handling as reconciliation_required, which
  -- represents an outcome that may already have reached WhatsApp and must
  -- never be auto- or silently re-opened.

  -- Database-layer defense in depth: quotations' own CHECK constraints
  -- already bound every one of these fields under normal operation, but
  -- this function never trusts that as the only line of defense before
  -- committing a durable delivery_pending reservation and (eventually)
  -- building a real WhatsApp send from this data. If any field somehow
  -- fails re-validation here (e.g. a CHECK constraint was bypassed),
  -- this returns invalid_database_state and creates NO delivery_pending
  -- row -- never a silently stuck reservation.
  IF NOT (v_quote.recipient_phone ~ '^[1-9][0-9]{7,14}$')
     OR NOT (length(v_quote.quotation_number) BETWEEN 1 AND 64 AND v_quote.quotation_number ~ '^[A-Za-z0-9._-]+$')
     OR v_quote.total_minor_units IS NULL OR v_quote.total_minor_units < 0 OR v_quote.total_minor_units > 999999999999
     OR NOT (v_quote.currency ~ '^[A-Z]{3}$')
     OR NOT (length(v_quote.link_token) BETWEEN 16 AND 128 AND v_quote.link_token ~ '^[A-Za-z0-9_-]+$')
  THEN
    UPDATE quotation_delivery_events SET result_status = 'invalid_database_state' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'owner_applied'::TEXT, 'invalid_database_state'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    RETURN;
  END IF;

  INSERT INTO quotation_delivery_state (quotation_id, status, active_idempotency_key, reserved_version, updated_at)
  VALUES (p_quotation_id, 'delivery_pending', p_idempotency_key, v_quote.version, now())
  ON CONFLICT (quotation_id) DO UPDATE
    SET status = 'delivery_pending', active_idempotency_key = p_idempotency_key,
        reserved_version = v_quote.version, updated_at = now();

  UPDATE quotation_delivery_events SET result_status = 'delivery_pending' WHERE idempotency_key = p_idempotency_key;

  RETURN QUERY SELECT 'owner_applied'::TEXT, 'delivery_pending'::TEXT,
    v_quote.recipient_phone, v_quote.quotation_number, v_quote.total_minor_units,
    v_quote.currency, v_quote.valid_until::TEXT, v_quote.link_token, v_quote.version;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION finalize_quotation_delivery(
  p_idempotency_key      TEXT,
  p_quotation_id         TEXT,
  p_outcome              TEXT,   -- 'sent' | 'failed' | 'invalid_database_state'
  p_provider_message_id  TEXT
) RETURNS TABLE (out_ok BOOLEAN, out_result_status TEXT) AS $$
DECLARE
  v_event quotation_delivery_events%ROWTYPE;
  v_state quotation_delivery_state%ROWTYPE;
  v_current_version INTEGER;
  v_new_status TEXT;
BEGIN
  IF p_outcome NOT IN ('sent', 'failed', 'invalid_database_state') THEN
    RETURN QUERY SELECT false, 'finalize_invalid_outcome'::TEXT;
    RETURN;
  END IF;

  -- Never trust the caller's outcome/provider-id pairing on the n8n side
  -- alone -- enforce the same relationship this function's own tables
  -- require (qds_provider_message_id_status_check) as an input-shape gate,
  -- before touching any row. Mutates nothing. 'invalid_database_state'
  -- means no sender call ever happened (the n8n-layer defense-in-depth
  -- gate rejected the send request before Call Sender), so it carries the
  -- same null-provider-id requirement as 'failed'.
  IF p_outcome = 'sent' AND (p_provider_message_id IS NULL
      OR NOT (length(p_provider_message_id) BETWEEN 1 AND 256 AND p_provider_message_id ~ '^[A-Za-z0-9_.=-]+$')) THEN
    RETURN QUERY SELECT false, 'finalize_invalid_provider_message_id'::TEXT;
    RETURN;
  END IF;

  IF p_outcome IN ('failed', 'invalid_database_state') AND p_provider_message_id IS NOT NULL THEN
    RETURN QUERY SELECT false, 'finalize_invalid_provider_message_id'::TEXT;
    RETURN;
  END IF;

  v_new_status := CASE WHEN p_outcome = 'sent' THEN 'sent' WHEN p_outcome = 'failed' THEN 'failed' ELSE 'invalid_database_state' END;

  SELECT * INTO v_event FROM quotation_delivery_events WHERE idempotency_key = p_idempotency_key FOR UPDATE;

  IF NOT FOUND THEN
    RETURN QUERY SELECT false, 'finalize_unknown_key'::TEXT;
    RETURN;
  END IF;

  IF v_event.quotation_id <> p_quotation_id THEN
    RETURN QUERY SELECT false, 'finalize_mismatch'::TEXT;
    RETURN;
  END IF;

  IF v_event.result_status <> 'delivery_pending' THEN
    -- Already finalized. Return the existing, real, durable result
    -- untouched -- idempotent replay, never able to flip an
    -- already-terminal outcome.
    RETURN QUERY SELECT true, v_event.result_status;
    RETURN;
  END IF;

  SELECT * INTO v_state FROM quotation_delivery_state WHERE quotation_id = p_quotation_id FOR UPDATE;

  IF NOT FOUND OR v_state.active_idempotency_key IS DISTINCT FROM p_idempotency_key OR v_state.status <> 'delivery_pending' THEN
    -- The state row no longer agrees with this key's own event row. Never
    -- touch quotation_delivery_state -- whatever currently owns it (a
    -- newer attempt, or nothing) is left completely alone.
    UPDATE quotation_delivery_events SET result_status = 'reconciliation_required' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT false, 'reconciliation_required'::TEXT;
    RETURN;
  END IF;

  -- Ownership integrity: both rows must carry a genuine, agreeing
  -- reserved_version before this attempt is trusted to finalize anything.
  -- A NULL on either side, or a disagreement between the event's own
  -- reserved_version and the state's, means the durable record of "what
  -- version this attempt observed" is not trustworthy -- never guess,
  -- always defer to reconciliation.
  IF v_event.reserved_version IS NULL OR v_state.reserved_version IS NULL OR v_event.reserved_version IS DISTINCT FROM v_state.reserved_version THEN
    UPDATE quotation_delivery_state SET status = 'reconciliation_required', updated_at = now()
      WHERE quotation_id = p_quotation_id AND active_idempotency_key = p_idempotency_key;
    UPDATE quotation_delivery_events SET result_status = 'reconciliation_required' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT false, 'reconciliation_required'::TEXT;
    RETURN;
  END IF;

  SELECT version INTO v_current_version FROM quotations WHERE quotation_id = p_quotation_id;

  IF v_current_version IS DISTINCT FROM v_event.reserved_version THEN
    UPDATE quotation_delivery_state SET status = 'reconciliation_required', updated_at = now()
      WHERE quotation_id = p_quotation_id AND active_idempotency_key = p_idempotency_key;
    UPDATE quotation_delivery_events SET result_status = 'reconciliation_required' WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT false, 'reconciliation_required'::TEXT;
    RETURN;
  END IF;

  UPDATE quotation_delivery_state
  SET status = v_new_status,
      provider_message_id = CASE WHEN p_outcome = 'sent' THEN p_provider_message_id ELSE NULL END,
      updated_at = now()
  WHERE quotation_id = p_quotation_id AND active_idempotency_key = p_idempotency_key;

  UPDATE quotation_delivery_events SET result_status = v_new_status WHERE idempotency_key = p_idempotency_key;

  RETURN QUERY SELECT true, v_new_status;
END;
$$ LANGUAGE plpgsql;
```

There is **no `p_expected_version` parameter** — the exact same correction already applied to `unpaid-invoice-reminder`'s `finalize_invoice_reminder` after a real ownership-verification defect was found there (see that workflow's own [Finalize-ownership correction](unpaid-invoice-reminder.md#finalize-ownership-correction)) is designed in from the start here: `finalize_quotation_delivery` locks and verifies the exact `quotation_delivery_events` row for the supplied `idempotencyKey`, requires it to exist and match the supplied `quotationId`, requires its `result_status` to still be `delivery_pending`, then locks `quotation_delivery_state` and requires `active_idempotency_key` to match the same key **and** `status` to still be `delivery_pending` — proof that this exact attempt, not a different or newer one, currently owns the row. Only then does it require **both** rows' `reserved_version` to be non-null and mutually agree, and checks the quotation's current version against that agreed, **durably stored** value — never a caller-supplied one, and never trusting either row's `reserved_version` alone. A nonexistent key, a mismatched `quotationId`, an invalid `p_outcome` value, or an outcome/`provider_message_id` combination that doesn't match the same relationship the schema itself enforces (`sent` requires a genuine non-null id; `failed` requires a null one) mutates nothing and returns a controlled failure (`finalize_unknown_key` / `finalize_mismatch` / `finalize_invalid_outcome` / `finalize_invalid_provider_message_id`). A replay against an already-terminal event returns that event's real, existing result unchanged — it can never flip `sent` to `failed`, `failed` to `sent`, or either into `reconciliation_required`.

Verified directly against Postgres, all reproduced against the real committed graph, not just the raw functions: a finalize call with a nonexistent key, with another real attempt's key (confirmed: attempting to hijack a *different*, genuinely in-flight quotation's delivery with a real key belonging to a different quotation — zero mutation on either row), with the correct key but a wrong quotation, and with an invalid outcome value all mutate nothing. A replay against an already-`sent` attempt trying to flip it to `failed` (and the reverse, against an already-`failed` attempt) leaves the state and `provider_message_id` completely unchanged. A stale attempt finalizing after a fresh reservation has taken over the same `quotation_id` (simulating a manual reconciliation freeing it) is rejected with the newer owner's row left byte-for-byte untouched. `sent` + null/empty/malformed/oversized `provider_message_id`, and `failed` + a non-null `provider_message_id`, are each rejected with zero mutation (`finalize_invalid_provider_message_id`); a genuine event/state `reserved_version` disagreement, and a null `reserved_version` on either row, both correctly degrade to `reconciliation_required` rather than being trusted. See [Test procedure](#test-procedure) for the complete adversarial suite.

## Known issue found and fixed during this build

**What was wrong:** the first version of `process_quotation_delivery` wrote `v_quote.status` directly into `quotation_delivery_events.result_status` whenever a quotation wasn't `approved`, trusting `quotations_status_check` as the only thing bounding that value to a known set. If `quotations_status_check` were ever bypassed at the database level (e.g. an administrator dropping the constraint, or a future migration mistake) and an out-of-enum `status` value existed on a row, that `UPDATE` would violate `quotation_delivery_events`'s own **separate, narrower** `qde_result_status_check` constraint and abort the entire statement with a hard Postgres error — surfacing as an n8n execution failure rather than a controlled `invalid_database_state` response. Found empirically, not by inspection alone: reproduced directly by temporarily dropping `quotations_status_check`, inserting a row with `status = 'totally_bogus_status'`, and calling `process_quotation_delivery` against it — confirmed the hard error, including through the real committed n8n graph (top-level execution `status: "error"`).

**The fix:** `v_quote.status` is now explicitly validated against the exact known set (`draft`/`cancelled`/`rejected`/`accepted`) before ever being written anywhere. An unrecognized value degrades to the explicit, honest `invalid_database_state` — now a formally allowed value in `qde_result_status_check` — instead of crashing. The corresponding n8n-layer allowlists (`Classify Reserve Result` and `Build Finalize Response`) were also updated to recognize `invalid_database_state` as a legitimate passthrough value rather than relying on it merely coinciding with their own fallback literal. Re-verified: the identical reproduction (bypassed constraint, bogus status, reservation attempt) now returns `owner_applied`/`invalid_database_state` at the SQL layer and a clean `{"status":"invalid_database_state",...}` controlled response through the real n8n graph, with the top-level execution completing successfully rather than erroring. Full regression (draft/cancelled/approved paths) re-confirmed unaffected.

This gap is not externally reachable — `p_quotation_id`/`p_idempotency_key`/`p_expected_version` are the only caller-influenced inputs, and none of them can produce an out-of-enum `quotations.status`; reaching this path requires the database's own primary constraint to already be bypassed. It was still worth closing, both because "unknown database values must fail closed" is a requirement of this workflow's own design, not only a defense against a hostile caller, and because a hard execution error is a strictly worse operator experience than a controlled, understandable response.

## Corrections applied after external adversarial review

Four release-blocking defects were found by a direct review of the initially committed workflow and fixed on the same branch before this package moved to Available. Each was reproduced against the actual committed SQL/graph first, fixed, and re-verified — both at the SQL layer and through the real committed n8n graph — before being considered closed.

**1. `reconciliation_required` could be silently reopened and resent by a fresh idempotency key.** `process_quotation_delivery` originally blocked only `state.status = 'sent'` and `'delivery_pending'` from reaching the reservation `INSERT ... ON CONFLICT DO UPDATE` — a `reconciliation_required` row fell through, and a fresh key would reopen it as a brand-new `delivery_pending` reservation, ready for a real send. Reproduced directly: seeded a `reconciliation_required` state row, called `process_quotation_delivery` with a fresh key, and confirmed it returned real quote data with a fresh `delivery_pending` reservation. **Fixed** by adding an explicit `v_state.status = 'reconciliation_required'` branch that records the fresh key's own event result but never touches `quotation_delivery_state` — re-verified both at the SQL layer and through the real graph: zero sends, the durable state row byte-for-byte unchanged. `failed` is deliberately **not** treated the same way — a definite, synchronous send failure remains safely retryable with a fresh key (see [Crash and reconciliation behavior](#crash-and-reconciliation-behavior)); only the genuinely ambiguous `reconciliation_required` state is blocked. Regression-tested: a `failed` state is still reopened correctly by a fresh key.

**2. Postgres-layer defense-in-depth data validation happened too late, leaking a permanently stuck `delivery_pending` reservation.** `process_quotation_delivery` committed `delivery_pending` before `Build Send Request`'s own re-validation of `linkToken`/`totalMinorUnits` ever ran; if that n8n-layer validation then rejected the data, `Build Invalid Send Request Result` returned `invalid_database_state` **without ever calling finalize** — leaving a durable `delivery_pending` row with no path to resolution, blocking every future attempt (including fresh keys) at that quotation forever. Reproduced directly: bypassed `quotations_link_token_check`, inserted a too-short token, called `process_quotation_delivery`, and confirmed the durable row was left at `delivery_pending` with a fresh-key retry also stuck at `delivery_pending` indefinitely. **Fixed** by adding the same field validation (`recipient_phone`, `quotation_number`, `total_minor_units`, `currency`, `link_token`) directly inside `process_quotation_delivery`, evaluated immediately before the reservation `INSERT` — an invalid value now returns `invalid_database_state` and creates **no** `quotation_delivery_state` row at all. (`valid_until` is not separately re-validated here: it is a `TIMESTAMPTZ` column, structurally guaranteed by the column type itself rather than by a `CHECK` constraint that could be dropped.) Re-verified both at the SQL layer and through the real graph: zero `quotation_delivery_state` rows, a clean `invalid_database_state` event, and a fresh-key retry afterward correctly reports `invalid_database_state` again rather than being stuck.

**3. `finalize_quotation_delivery` under-enforced the outcome/provider-id relationship and the reservation's own version integrity.** The function accepted `p_outcome = 'sent'` with a `NULL` or malformed `p_provider_message_id`, relying entirely on `Classify Send Result`'s n8n-layer validation rather than enforcing it itself — reproduced directly by calling `finalize_quotation_delivery('sent', NULL)` at the SQL layer, bypassing n8n entirely, and confirming it was accepted and durably recorded. It also checked the quotation's current version only against `quotation_delivery_state.reserved_version`, never against `quotation_delivery_events.reserved_version`, and never asserted either was non-null. **Fixed**: `finalize_quotation_delivery` now independently validates the outcome/provider-id relationship before touching any row (`sent` requires a genuine `^[A-Za-z0-9_.=-]+$` id of length 1–256; `failed` requires a null one — mutating nothing on failure, `finalize_invalid_provider_message_id`), the same relationship is additionally enforced as a `CHECK` constraint on `quotation_delivery_state` itself (`qds_provider_message_id_status_check`), and both rows' `reserved_version` must be non-null and mutually agree before the current quotation version is even checked. **A real implementation defect was caught by live-testing this fix itself**: the first draft used a `{1,256}` bound quantifier in a PL/pgSQL regex (`^[A-Za-z0-9_.=-]{1,256}$`), which threw `regular expression geçersiz: invalid repetition count(s)` — PostgreSQL's regex engine hard-caps repetition-count bounds at 255. Rewritten to `length(...) BETWEEN 1 AND 256 AND ... ~ '^[A-Za-z0-9_.=-]+$'`, matching the pattern the schema's own `CHECK` constraints already used for exactly this reason. All 8 mandatory cases (`sent`+null/empty/malformed/oversized id, `failed`+non-null id, event/state `reserved_version` disagreement, null `reserved_version` on either row, and the correct-owner happy path) verified directly against Postgres — every invalid case mutates nothing and the state stays `delivery_pending`.

**4. The shipped configuration placeholder did not fail closed.** `QUOTATION_LINK_ORIGIN` (`'https://quotes.example.com/q/'`) is syntactically valid HTTPS — a deployment that forgot to replace it would reach a real WhatsApp send carrying a broken `example.com` link, since nothing previously distinguished "a real configured origin" from "the shipped placeholder." **Fixed** by adding `Load Configuration`, a new node that runs before any Postgres call, rejecting the shipped `PHONE_NUMBER_ID` and `QUOTATION_LINK_ORIGIN` placeholders with a controlled `configuration_required` result — zero database mutations, zero sender calls. **A real portability defect was caught by live-testing this fix itself**: the first draft's origin validator used `new URL(origin)`, which threw `ReferenceError: URL is not defined` inside n8n's own Code node sandbox — confirmed directly (`typeof URL` is `'undefined'` there, though it is an ordinary Node.js global everywhere else) — silently routing every request to `configuration_required` regardless of the configured origin's actual validity. This validator (both the placeholder-detection logic and the `URL`-avoidance fix) was later superseded by a substantially more thorough one — see [Corrections applied after a second external adversarial review](#corrections-applied-after-a-second-external-adversarial-review), finding 3.

**`quotations.link_token` now carries a `UNIQUE` constraint** (`quotations_link_token_unique`), not only the format `CHECK` — no two quotations can ever be issued the same delivery link.

**The expiry boundary changed from exclusive to inclusive** (`valid_until < now()` → `valid_until <= now()`): a quotation whose valid-until instant is exactly "now" is treated as already expired, not valid for one more instant. Verified directly with a quotation seeded at `valid_until = now()` at insert time — reports `expired`.

## Corrections applied after a second external adversarial review

Three further release-blocking defects were found by a direct review of the corrections above and fixed on the same branch. Each was reproduced against the actual committed SQL/graph first, fixed, and re-verified — both at the SQL layer and through the real committed n8n graph — before being considered closed.

**1. `valid_until` was not actually validated — PostgreSQL's `infinity`/`-infinity` timestamptz values bypassed the expiry check.** The documentation claimed every database-derived send field was validated before `delivery_pending` was created, but `process_quotation_delivery` only ever checked `valid_until <= now()`. `'infinity'::timestamptz <= now()` is `false` (infinity is never in the past), so an `infinity` `valid_until` passed the expiry check and would have reached the WhatsApp template as the literal string `"infinity"`; `'-infinity'::timestamptz <= now()` is `true`, so a `-infinity` value was mislabeled `expired` rather than honestly reported as corrupt data. Reproduced directly: seeded quotations with `valid_until = 'infinity'` and `'-infinity'` (via a temporary constraint bypass — `quotations_valid_until_finite_check` itself already blocks this at `INSERT` time under normal operation) and confirmed both outcomes. **Fixed** with `quotations_valid_until_finite_check CHECK (isfinite(valid_until))` at the schema layer, plus an explicit `NOT isfinite(v_quote.valid_until)` re-check inside `process_quotation_delivery` — checked *before* the expiry comparison, so both directions degrade uniformly to `invalid_database_state` rather than one of them slipping through as `expired`. Re-verified: both `infinity` and `-infinity` now return `owner_applied`/`invalid_database_state` with zero `quotation_delivery_state` rows created, at the SQL layer and live through the real committed graph (via a mock-bound copy with a real configuration, since the shipped committed workflow's placeholder config would otherwise mask this test behind `configuration_required`). Regression-confirmed: a normal past-dated `valid_until`, and the existing `valid_until = now()` inclusive boundary, are both unaffected.

**2. The n8n-layer defense-in-depth rejection still stranded a `delivery_pending` reservation.** `Build Send Request` can route to a false branch after `process_quotation_delivery` has already committed `delivery_pending` — structurally should be unreachable now that the database layer validates the same fields itself (see finding 2 in the corrections above), but the review required this defense-in-depth path to have honest durable semantics regardless, since it is also a general-purpose function another caller could invoke directly. Previously this branch only returned `invalid_database_state` as a plain JSON response, without ever calling finalize — leaving the event and state rows stuck at `delivery_pending` with no path to resolution. **Fixed** by adding `Finalize Invalid Send Request`, a new Postgres node between the false branch and the response builder, calling the same `finalize_quotation_delivery` function used for real sends with a new third outcome, `'invalid_database_state'` (provider-message-id always `NULL`, since no sender call ever happened) — reusing its full ownership-verification logic rather than inventing a separate cleanup path. `finalize_quotation_delivery` now accepts `p_outcome IN ('sent', 'failed', 'invalid_database_state')`; `quotation_delivery_state.status` now allows `'invalid_database_state'` as a fourth value, and `process_quotation_delivery` blocks it from ever being silently reopened by a fresh key, the same way `reconciliation_required` is blocked — but as its own distinct state, since zero sender calls ever occurred here (no "may have already reached WhatsApp" ambiguity, just corrupt data needing manual correction). **Live-tested via a genuine, temporary test-only mismatch** between database acceptance and n8n validation: temporarily replaced `process_quotation_delivery` with a variant missing its own `link_token` re-validation (leaving the table's `CHECK` constraint as the only line of defense, itself bypassed for this one test), reserved a corrupted quotation, and confirmed the real graph now correctly finalizes it — `quotation_delivery_state.status` moved from `delivery_pending` to `invalid_database_state`, `quotation_delivery_events.result_status` recorded the same, zero mock sender calls, and a fresh-key retry was correctly blocked rather than silently resending. The test-only function variant was replaced with the real one immediately after, re-confirmed by reproducing the original reservation-time rejection again.

**3. Deployment configuration validation was incomplete.** `Load Configuration` (added in the corrections above) validated only `PHONE_NUMBER_ID` (against a placeholder set) and `QUOTATION_LINK_ORIGIN` (against a regex that accepted invalid/deceptive hosts — leading/trailing dots, consecutive dots, labels starting/ending with a hyphen, and any host merely containing the string "example" rather than only genuine subdomains of the reserved example domains). `GRAPH_API_VERSION`, `TEMPLATE_NAME`, and `LANGUAGE_CODE` were not validated for format at all. **Fixed**: every one of the five hardcoded configuration values is now validated — see [Link security design](#link-security-design) for the full detail, including the hostname validator rewritten with plain string splitting and per-label checks (not one large nested regex, per explicit review requirement) and its dot-boundary-correct reserved-domain check. This validator (both the `PHONE_NUMBER_ID` placeholder check and the hostname/IP-literal logic) was later found to still have two bypasses — see [Corrections applied after a third external adversarial review](#corrections-applied-after-a-third-external-adversarial-review).

## Corrections applied after a third external adversarial review

Two further bypasses in the [second review's](#corrections-applied-after-a-second-external-adversarial-review) configuration validator were found by an isolated execution of `Load Configuration` and fixed on the same branch. Each was reproduced against the actual committed node code first, fixed, and re-verified — both in isolation and through the real n8n Code-node execution environment (not plain Node.js alone, since an earlier round's `URL`-global defect already proved isolated testing insufficient) — before being considered closed.

**1. `PHONE_NUMBER_ID` values consisting entirely of zeroes passed unless they exactly equalled the one 15-zero placeholder string.** `PLACEHOLDER_PHONE_NUMBER_IDS` was a `Set` containing exactly `'000000000000000'` — any other all-zero, digits-only-format-valid string (`'00000'`, `'00000000000000000000'`) was accepted, since it simply wasn't in the set. Reproduced directly: both values passed the combined format-and-placeholder check. **Fixed** by replacing the exact-match set lookup with a general `isAllZeroDigits(v)` check (`/^0+$/`) — rejects every all-zero value regardless of length, not one specific string. Re-verified: `'00000'`, the original 15-zero placeholder, and `'00000000000000000000'` are all rejected; a genuine non-zero production-shaped id (`'15511111111'`) still passes.

**2. The hostname validator rejected only canonical four-part IPv4 values.** `isIpLiteral` required `labels.length === 4` before even checking whether every label was numeric — a 2-label numeric host like `'127.1'` never reached the numeric check at all (`labels.length !== 4` short-circuited to "not an IP literal"), and even within the 4-label branch, a label failing the `<= 255` range check (like `'999'` in `999.999.999.999`) made `.every(...)` return `false`, which this function then read as "not an IP literal" rather than "still an invalid, IP-shaped host." Reproduced directly: both `'127.1'` and `'999.999.999.999'` passed `isValidHostname` unmodified. **Fixed** by replacing `isIpLiteral`'s dotted-quad-specific logic with two independent, more general checks inside `isValidHostname` itself: `isNumericOnlyHost` rejects any host where **every** label is purely numeric, regardless of label count (catching the canonical form, the shortened form, the out-of-range form, and any other all-numeric-label shape with one rule); and a **final-label-must-contain-a-letter** check (`hasLetter`) as an independent second line of defense, since a real TLD is never purely numeric. Together these still correctly preserve a genuine numeric *subdomain* of an otherwise ordinary hostname — `123.quotes.company.com` has a non-numeric final label and is not all-numeric as a whole, so it is accepted — because the rule targets the complete hostname's shape, not any individual label in isolation. IPv6/colon rejection is unaffected (already enforced earlier in `isValidOrigin`, before `isValidHostname` is ever called). Re-verified against 13 mandatory focused cases (both all-zero-phone and numeric-host variants) run inside a real n8n Code node via `n8n execute`, not just isolated Node.js.

**Also required and fixed alongside the above:** path segments equal to `.` or `..` are now rejected outright (`isValidPathSegment`), replacing the single whole-path regex with a segment-by-segment check — a client or intermediary that normalizes `/../` in the path can no longer alter which page the configured quotation base path actually points to, since that shape is never accepted as configuration in the first place.

## Sender sub-workflow binding

`Call Sender` is an Execute Workflow node with `source: "database"` and `workflowId: {"mode": "id", "value": "R1QDUW9jYqxREyDS", "cachedResultName": "WhatsApp Template Message Sender"}` — the exact same reference mechanism [`whatsapp-appointment-reminder`](whatsapp-appointment-reminder.md) and [`unpaid-invoice-reminder`](unpaid-invoice-reminder.md) already established and documented in detail. Re-verified directly for this workflow: imported the real, unmodified sender alongside this workflow into a fresh instance and confirmed the reference resolves without manual rebinding, and re-confirmed the same on a second, independently clean instance after a full CLI export/import round trip. See [Clean re-import results](#clean-re-import-results). **If you re-export or otherwise change the sender workflow in a way that changes its `id`,** this reference will break explicitly (Execute Workflow surfaces a clear error, it does not fail open) — update `Call Sender`'s `workflowId.value` to match.

## Sender-output fail-closed design

`Classify Send Result` never trusts the sender's `status` field alone. The sender's own controlled contract (`{status, httpStatus, providerMessageId}`) can legitimately return `status: 'sent'` with `providerMessageId: null` for a malformed 2xx provider response (its own documented behavior — see [`whatsapp-template-message-sender.md`](whatsapp-template-message-sender.md)). A send is only ever treated as successful when **all three** fields agree: `status` is exactly `'sent'`, `httpStatus` is a genuine 2xx integer, and `providerMessageId` is a non-empty, bounded (1–256 character), safely-charactered identifier (`^[A-Za-z0-9_.=-]+$`) — the same policy `unpaid-invoice-reminder` already established for this repository, reused here rather than reinvented. Anything short of all three becomes `failed`; a non-`sent` outcome never retains a `providerMessageId`. Verified directly against the real graph: the sender's documented malformed-2xx behavior correctly produces `failed` with `providerMessageId: null`, while a genuinely valid send is unaffected.

## Crash and reconciliation behavior

The complete state machine this workflow can leave a quotation in:

| State | Meaning |
|---|---|
| `configuration_required` | This workflow's own hardcoded deployment configuration (WhatsApp/Graph API settings, the quotation-link origin) is missing or still a shipped placeholder — checked by `Load Configuration` **before** the atomic database write ever runs. Zero database mutations, zero sender calls. See [Link security design](#link-security-design). |
| *(no `quotation_delivery_events` row)* | This `idempotencyKey` was never durably reserved — either rejected before the atomic write (invalid input, or a rejected configuration), or the atomic write never ran (e.g. Postgres was unavailable). |
| `missing_quotation` | The atomic write ran; no quotation with this id exists. |
| `conflict` | The atomic write ran; `expectedVersion` did not match the quotation's current version. |
| `draft` / `cancelled` / `rejected` / `accepted` | The atomic write ran; the quotation's real, current status makes delivery inappropriate. |
| `expired` | The atomic write ran; the quotation's valid-until instant is now in the past **or exactly now** (inclusive boundary — checked against Postgres's own clock). |
| `already_delivered` | The atomic write ran; this quotation was already successfully delivered. |
| `delivery_pending` | The atomic write committed this durable pending marker before any WhatsApp call was attempted — reported both as the reservation owner's own in-flight state and to any other concurrent/duplicate request. This is the only state a crash between the database write and the send call (or between the send call and finalizing its result) can leave visible. |
| `sent` | The send succeeded and was finalized via the ownership-verified, version-guarded write — full success. |
| `failed` | The send was attempted and definitively failed (any non-2xx status, a transport failure, or an internally-inconsistent malformed `sent` response), finalized via the ownership-verified write. **Retriable via a fresh `idempotencyKey`** — deliberately, since a synchronous non-2xx/transport-failure response means WhatsApp told us (or we can be reasonably confident) the message was not accepted. This is **not** the same as `reconciliation_required` below, and a fresh key targeting a `failed` quotation is allowed to open a brand-new reservation — verified directly, and re-confirmed as a regression check after the fix described in [Corrections applied after external adversarial review](#corrections-applied-after-external-adversarial-review). |
| `reconciliation_required` | An **ambiguous** outcome: either the finalize call's ownership check failed (the event row no longer agreed with the state row it should own), the quotation moved on since this attempt's reservation, or the two reservation rows' `reserved_version` values were missing or disagreed. The row that failed ownership/version verification is left completely untouched, and — unlike `failed` — a fresh `idempotencyKey` targeting this quotation is explicitly blocked from opening a new reservation; a human or a separate reconciliation process must resolve this by hand first. See [Corrections applied after external adversarial review](#corrections-applied-after-external-adversarial-review) for the defect this closes. |
| `invalid_database_state` as a durable `quotation_delivery_state.status` | **Not ambiguous** — zero sender calls ever occurred. Reached only via `Finalize Invalid Send Request`, the ownership-verified cleanup for the (structurally-should-be-unreachable) n8n-layer defense-in-depth rejection: `process_quotation_delivery` committed `delivery_pending`, but `Build Send Request`'s own re-validation then rejected the data anyway. The state is moved out of `delivery_pending` into this explicit manual-resolution state — a human must correct the underlying quotation's data before any further delivery attempt; a fresh `idempotencyKey` is explicitly blocked from reopening it, the same way `reconciliation_required` is. `invalid_database_state` also appears as an *event-only* outcome (no `quotation_delivery_state` row at all) when `process_quotation_delivery` itself rejects corrupt data or a non-finite `valid_until` before ever creating a reservation — see [Corrections applied after a second external adversarial review](#corrections-applied-after-a-second-external-adversarial-review). |

Two further outcomes (`finalize_unknown_key`, `finalize_mismatch`) and two input-validation outcomes (`finalize_invalid_outcome`, `finalize_invalid_provider_message_id`) exist only as defense-in-depth responses from `finalize_quotation_delivery` itself against a finalize call with a nonexistent `idempotencyKey`, one that belongs to a genuinely different `quotationId`, a garbage `outcome` value, or an outcome/`provider_message_id` combination that violates the schema's own relationship between them — none of these are reachable through this workflow's own committed graph under normal *sending* operation, but the function refuses them regardless, mutating nothing, since it is also a general-purpose database function another caller could invoke directly. `finalize_quotation_delivery` accepts three outcomes overall: `'sent'`, `'failed'`, and `'invalid_database_state'` (the last used only by `Finalize Invalid Send Request`, never by a real send).

**If the process crashes after the atomic database write commits but before or during the send call**, the database durably and visibly shows `delivery_pending`. A duplicate request for the same `idempotencyKey` reports this exact pending state and makes zero new sends. A fresh `idempotencyKey` for the same `quotationId` is rejected as `delivery_pending` (if still genuinely in flight) or `already_delivered` (if it has since completed) — this workflow never allows a second overlapping attempt at the same quotation regardless of which idempotency key is used.

**If the send itself succeeds or fails but the final Postgres write then fails** (e.g. Postgres becomes unreachable at exactly that moment), this workflow does **not** automatically retry the send — the message may already have reached WhatsApp, and retrying blindly risks a duplicate. The execution fails loudly (no `continueOnFail` on the finalize node), and the delivery state is left showing `delivery_pending` — its state from the initial atomic write — until a human or a separate reconciliation process checks WhatsApp's actual delivery state and updates Postgres by hand. Verified directly by isolating the finalize write against a deliberately unreachable Postgres connection after a successful mock send: exactly one send occurred, and the state remained durably `delivery_pending` — never falsely marked `sent`.

**Resolving a stuck `delivery_pending`, `reconciliation_required`, or `invalid_database_state` row is outside this workflow's scope.** The first two require checking the actual delivery state on WhatsApp's side; `invalid_database_state` instead requires correcting the underlying quotation's own data (it never reached the sender at all). In every case this means manually updating `quotation_delivery_state` (and, if appropriate, `quotation_delivery_events.result_status`) — this workflow deliberately does not attempt that automatically.

## Controlled output contract

Every execution branch returns exactly the same four-field shape — proven by construction (every terminal Code node in this workflow builds this exact shape, nothing more) and by test: `status`, `quotationId`, `httpStatus`, `providerMessageId`. **Never** the recipient phone, quotation number, amount, currency, valid-until date, the quotation link or its token, template parameters, credentials, raw provider responses, or SQL/database errors. `httpStatus`/`providerMessageId` are `null` on every path that never reaches the sender (all rejection/ineligibility/duplicate paths, and the `Send Request Valid?` false branch).

## Required credentials

**One**, not included in the exported JSON — no node has a credential bound after import, by design:

| Credential | Bound to node(s) | Type |
|---|---|---|
| e.g. "Quotations Postgres" | `Reserve And Apply`, `Finalize Invalid Send Request`, `Finalize Send Result` | n8n **Postgres** credential (`postgres`), pointed at your own database with the schema and functions in [Atomic idempotency and ownership design](#atomic-idempotency-and-ownership-design) |

The sender's own credential (its HTTP Header Auth WhatsApp access token) lives only in the already-committed [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) workflow — this workflow never touches it directly.

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Setup steps

1. Create the schema — see [Atomic idempotency and ownership design](#atomic-idempotency-and-ownership-design) for a new installation.
2. Import `customer-quotation-delivery.json`.
3. Create and bind your Postgres credential (see [Required credentials](#required-credentials)) to `Reserve And Apply`, `Finalize Invalid Send Request`, and `Finalize Send Result`.
4. Ensure [`whatsapp-template-message-sender.json`](whatsapp-template-message-sender.json) is already imported (with its own WhatsApp credential bound) — if you imported it via the official CLI or the editor's normal import feature without changing its id, `Call Sender`'s reference resolves automatically.
5. **Edit `Load Configuration`'s hardcoded constants before real use**: `GRAPH_API_VERSION` to Meta's currently-supported Graph API version, `PHONE_NUMBER_ID` to your real WhatsApp Business phone number id, `TEMPLATE_NAME` to your own real, Meta-approved template name, `LANGUAGE_CODE` to your template's language, and — critically — `QUOTATION_LINK_ORIGIN` to your own real, HTTPS quotation-viewing page's origin (a fixed scheme + a well-formed, multi-label hostname + a trailing-slash path — see [Link security design](#link-security-design) for the exact format required). This workflow ships with placeholder values, and `Load Configuration` will refuse to proceed past any of them — `configuration_required`, zero database or sender calls — until every one is replaced with a real, correctly formatted value (`Build Send Request` reads every one of these values from `Load Configuration`'s output, so there is only one place to edit).
6. Populate `quotations` from your real quotation data however you already do that — this workflow does not create quotations, calculate their contents, or generate `link_token` values itself; your application must generate a genuinely unguessable, unique token per quotation and store it there.
7. Build whatever calls this sub-workflow — a sales action or automated job that decides *which* approved quotation is worth delivering right now, and calls this workflow once per candidate, passing a fresh `idempotencyKey` per attempt, the `quotationId`, and `expectedVersion` looked up from your own quotation records. **Do not pass a recipient phone, quotation values, or a link** — none of these are part of this workflow's input contract.
8. Have a plan for resolving `delivery_pending`/`reconciliation_required`/`invalid_database_state` rows that don't clear on their own — see [Crash and reconciliation behavior](#crash-and-reconciliation-behavior). This workflow does not do this automatically.
9. Test with synthetic data against your own isolated setup first.

## Test procedure

Built and verified in an isolated local n8n test environment (the official `n8n` npm package pinned to v2.35.4 under Node.js v22.23.2 via `nvm`, isolated `N8N_USER_FOLDER`) against isolated local **PostgreSQL 16.15** instances (fresh `initdb` clusters, non-default ports, short unix-socket paths, synthetic data only). **The real Meta/WhatsApp API was never contacted** — every test requiring a send used a temporary, uncommitted mock-bound copy of both this workflow and the sender, targeting a local mock HTTP server instead of `https://graph.facebook.com`, never exported or committed. Confirmed by inspecting the mock server's own request log after every test run: every request target was `127.0.0.1`, never `graph.facebook.com`.

**CLI-only test methodology**, matching the already-established approach in `unpaid-invoice-reminder.md`: since `n8n execute --id` does not accept custom trigger input directly, each scenario used a small, throwaway, never-committed "test caller" workflow (Manual Trigger → Code node with the scenario's fixed input → Execute Workflow node calling the real target by id with auto-mapped input). Genuine concurrent execution used `n8n execute-batch --ids=A,B --concurrency=2` in one process, since two separate `n8n execute` processes each start their own internal Task Broker and collide.

### Core eligibility, validation, and duplicate handling

| # | Test | Result | Verified via |
|---|---|---|---|
| 1 | Valid approved, non-expired quotation | Exactly one mock send; `sent` with a real provider message id | mock-bound copy, real n8n execution |
| 2 | Draft quotation | `draft`, zero sends | real committed file, real n8n execution |
| 3 | Cancelled quotation | `cancelled`, zero sends | real committed file, real n8n execution, direct SQL |
| 4 | Rejected quotation | `rejected`, zero sends | real n8n execution, direct SQL |
| 5 | Accepted quotation | `accepted`, zero sends | real n8n execution, direct SQL |
| 6 | Expired quotation | `expired`, zero sends | direct SQL |
| 7 | Missing quotation | `missing_quotation`, zero sends | direct SQL, real n8n execution |
| 8 | Stale `expectedVersion` | `conflict`, zero sends | direct SQL |
| 9 | Missing `idempotencyKey`/`quotationId`/`expectedVersion` individually (3 cases), path-traversal-shaped id, 200-char oversized id, empty-string id, `expectedVersion` of `0`/`-1`/`1.5`/`"1"` (wrong type)/`99999999999` (oversized) | Every case: `rejected`, `quotationId` nulled out when the id itself was invalid, zero Postgres mutation (`quotation_delivery_events` row count unchanged across the batch) | real n8n execution (12 cases) |
| 10 | An unexpected extra field in the trigger input (e.g. `extraUnexpectedField`, or a caller-supplied `linkToken`/`quotationUrl`) | Harmlessly ignored — `Validate Input` only ever reads the three declared fields by name; a valid request alongside the extra field reserves normally, and a genuinely eligible request still sends using only the real Postgres-sourced link token | real n8n execution |
| 11 | Sequential duplicate (same `idempotencyKey` replayed after a completed `sent`) | Zero additional sends; the real, prior stored status (`sent`) is returned, never a generic label (`httpStatus`/`providerMessageId` correctly `null` on the replay path, matching the terminal-response builder's own contract) | direct SQL, real n8n execution |
| 12 | Concurrent identical duplicate (same `idempotencyKey`, genuinely racing) | Exactly one send (mock call-count delta = 1); the loser reports the winner's real status via `duplicate_match`, never `NULL`/stale | direct SQL (`BEGIN`/`pg_sleep`/concurrent-session technique), `n8n execute-batch --concurrency=2` against the real mock-bound graph |
| 13 | Same `idempotencyKey`, different `quotationId` | `idempotency_mismatch`, zero mutation/send for the loser — confirmed live: the target quotation's `quotation_delivery_state` row doesn't exist at all afterward | direct SQL, real n8n execution |
| 14 | Delivery already pending (fresh key, same quotation still genuinely in flight) | `delivery_pending`, zero sends | direct SQL |

### Money, link safety, and sender-output fail-closed suite

| # | Test | Result | Verified via |
|---|---|---|---|
| 15 | Money: Postgres `BIGINT` arriving as a JS string | Correctly formatted (`129900` → `"1299.00"`), confirmed live end-to-end after the fix (see [Money representation](#money-representation) for the defect this test caught) | real n8n execution, standalone unit test (15 cases, including string-shaped inputs) |
| 16 | Link safety: `link_token` bypassing the database `CHECK` constraint with a URL-shaped value | Caught by the n8n-layer `Send Request Valid?` gate — `invalid_database_state`, zero mock calls | real n8n execution (constraint temporarily dropped for this test only, then restored), direct SQL (path-traversal-shaped, URL-shaped, userinfo-shaped, and too-short tokens all rejected at `INSERT` time) |
| 17 | `sent` + null/empty-string/invalid-character/oversized (300-char) `providerMessageId` (4 cases) | Every case: `failed`, never `sent`; `providerMessageId` null | real n8n execution (null case), unit test (empty/invalid-character/oversized cases) |
| 18 | `sent` + non-2xx `httpStatus`; 2xx `httpStatus` with non-`sent` status | Both combinations are **structurally impossible to produce via the real, unmodified sender** — its own classification logic sets `status:'sent'` if and only if `httpStatus` is 2xx, in one unmodified if/else chain (confirmed by direct code inspection). `Classify Send Result` was still confirmed to fail closed for both anyway via direct unit test, as defense in depth against a hypothetically different sender | direct code inspection of the sender, unit test |
| 19 | Provider HTTP `400`/`401`/`429`/`500`, and a transport timeout (5 cases) | Every case, each on its own fresh quotation: `failed` with the correct `httpStatus` (`null` for the timeout), `quotation_delivery_state` lands on `failed`. Exactly 5 mock calls for 5 tests — no retries | mock-bound copy, real n8n execution |
| 20 | Malformed sender output (the sender's own documented 2xx-missing-`messages`-field behavior) | `failed`, never `sent`; `providerMessageId` null | real n8n execution |

### Finalize-ownership and replay-protection adversarial suite

| # | Test | Result | Verified via |
|---|---|---|---|
| 21 | Finalize: nonexistent key, another real key belonging to a different (genuinely in-flight) quotation, correct key wrong quotation, invalid outcome value | All four: zero mutation on any row, controlled failure (`finalize_unknown_key`/`finalize_mismatch`/`finalize_invalid_outcome`) | direct SQL |
| 22 | Finalize replay: `sent`→`failed` attempted, `failed`→`sent` attempted | Both: existing result returned unchanged, `provider_message_id` untouched | direct SQL |
| 23 | Stale finalize after a newer attempt has taken ownership of the same `quotationId` | The stale attempt's own event marked `reconciliation_required`; the newer, currently-owning attempt's state confirmed byte-for-byte unchanged | direct SQL |
| 24 | Quotation version changes between reservation and finalization | `reconciliation_required`; prior reservation's state left untouched | direct SQL |

### Database constraints, malformed database state, sensitive data, persistence, and portability

| # | Test | Result | Verified via |
|---|---|---|---|
| 25 | Database `CHECK` constraint sweep (identifier/status/phone/amount/currency/link-token/version/`quotation_number`/oversized `provider_message_id`/oversized `active_idempotency_key` shapes across all three tables — 17 cases total) | Every case rejected at `INSERT`/`UPDATE` time with the expected constraint name | direct SQL |
| 26 | Out-of-enum `quotations.status` reached after bypassing `quotations_status_check` | **A real defect found and fixed during this build** — see [Known issue found and fixed during this build](#known-issue-found-and-fixed-during-this-build). Before the fix: a hard Postgres error (the value violated `quotation_delivery_events`'s own separate `qde_result_status_check`), surfacing as an n8n execution failure. After the fix: a clean `invalid_database_state` controlled response, confirmed both at the SQL layer and through the real committed n8n graph (top-level execution completes successfully) | direct SQL, real n8n execution (both before and after the fix) |
| 27 | Controlled output contains no sensitive fields | Every seeded quotation's recipient phone, amount, quotation number, and link token, checked against the final controlled output of every test execution across this whole build (28+ executions): zero matches | real n8n execution |
| 28 | Execution-data persistence, verified through a live n8n **server** (not CLI-execute alone) with a session-authenticated REST API call and a unique canary `idempotencyKey` | Honestly separated, matching the evidentiary standard `unpaid-invoice-reminder.md` already established: (1) **API unavailability** — confirmed, `GET /rest/executions/:id` for the canary's own genuinely-completed execution returned `{}`, absent from the filtered executions list entirely; (2) **soft-deletion** — confirmed via direct SQLite inspection after `PRAGMA wal_checkpoint(FULL)`: the row shows `status:"running"`/`finished:0` but `deletedAt` already stamped at completion time, `execution_data` still present at that point; (3) **physical deletion** — **not observed**: after restarting with accelerated-pruning settings (`EXECUTIONS_DATA_PRUNE=true`, zero-length max-age/buffer/interval) and a real observation window, both the `execution_entity` row and its `execution_data` payload were still physically present; no pruning-related log lines appeared. Reported exactly as observed, not assumed | real n8n server (session-cookie authenticated REST API), direct SQLite inspection, `PRAGMA wal_checkpoint(FULL)`, accelerated-pruning observation window |
| 29 | Official CLI export/import | `n8n export:workflow --id=<id> --output=<dir>/ --separate --pretty` into a clean instance, content-diffed field-by-field against the hand-built draft (identical), re-exported from a **second**, fully independent clean instance with a freshly-created Postgres database and the schema reinstalled: `nodes`, `connections`, `settings` byte-for-byte identical at every hop. The sender-by-id reference resolved on the second instance with zero manual rebinding, confirmed both structurally (draft path) and behaviorally (mock-bound send path: `sent`, a real provider message id, exactly one mock call) | real committed file, second fully independent clean instance |

### Corrections retest — external adversarial review

A follow-up round after all four findings in [Corrections applied after external adversarial review](#corrections-applied-after-external-adversarial-review) were fixed. Run against a fresh Postgres instance with the corrected schema, and a fresh n8n test instance with the corrected workflow re-imported (real committed file and a re-derived mock-bound copy, credentials and mock wiring reapplied identically to the original round).

| # | Test | Result | Verified via |
|---|---|---|---|
| 30 | **Finding 1** — `reconciliation_required` seeded with an existing owner/version/state, then a fresh key attempts delivery | Zero sends (mock call count unchanged), durable state row byte-for-byte unchanged, the fresh key's own event records its own non-owning `reconciliation_required` result | direct SQL, real n8n execution against the real committed graph |
| 31 | Regression: a `failed` state is still correctly reopened by a fresh key (must not be conflated with `reconciliation_required`) | Fresh reservation succeeds, real quote data returned, ready to send | direct SQL |
| 32 | **Finding 2** — `link_token` CHECK bypassed with a too-short value, reservation attempted | `invalid_database_state`, **zero** `quotation_delivery_state` rows created (previously: a permanently stuck `delivery_pending` row) — confirmed both at the SQL layer and through the real committed n8n graph | direct SQL, real n8n execution |
| 33 | Fresh-key retry against the same corrupted quotation after the fix | Still cleanly `invalid_database_state`, not stuck at `delivery_pending` | real n8n execution |
| 34 | **Finding 3** — direct-SQL finalize matrix: `sent`+null/empty/malformed/oversized (300-char) `provider_message_id`; `failed`+non-null `provider_message_id`; event/state `reserved_version` disagreement; null `reserved_version` on the state row; null `reserved_version` on the event row; correct-owner/version happy path (8 cases) | All 7 invalid cases: zero mutation, `finalize_invalid_provider_message_id` or `reconciliation_required` as appropriate, state remains `delivery_pending`. Happy-path case: `sent`, provider id recorded correctly | direct SQL |
| 35 | Regression: stale key finalizing after a genuinely different, newer key has taken real ownership of the same quotation (both rows freshly reserved, not hand-seeded) | Stale attempt's own event → `reconciliation_required`; newer owner's row byte-for-byte unchanged; the newer owner can still finalize normally afterward | direct SQL |
| 36 | **A real implementation defect in the Finding-3 fix itself**, caught by this live testing: a `{1,256}` bound quantifier in a PL/pgSQL regex threw `invalid repetition count(s)` (Postgres's regex engine caps bound quantifiers at 255) | Confirmed via a minimal repro (`SELECT 'abc' ~ '^[A-Za-z0-9]{1,256}$'` fails; `{1,255}` succeeds); rewritten to `length(...) BETWEEN ... AND ... ~ '^[...]+$'`; re-verified against all 8 cases in row 34 with zero errors | direct SQL |
| 37 | **Finding 4** — the shipped placeholder configuration (`QUOTATION_LINK_ORIGIN`, `PHONE_NUMBER_ID`), unmodified, against the real committed workflow | `configuration_required`, zero Postgres calls (no `quotation_delivery_events` row created for a quotation that would otherwise not even need to exist), zero sender calls | real n8n execution against the real, unmodified committed file |
| 38 | A mock-bound copy with a real, non-placeholder configuration | A genuine successful send (`sent`, HTTP 200, a real mock provider message id) — confirms the gate only blocks the shipped placeholder, not configuration in general | mock-bound copy, real n8n execution |
| 39 | **A real portability defect in the Finding-4 fix itself**, caught by this live testing: the first draft's origin validator used `new URL(...)`, which threw inside n8n's Code node sandbox | Isolated repro: a debug workflow printed `typeof URL` as `'undefined'` and caught a `ReferenceError` from `new URL(...)`, executed through the real n8n CLI — confirmed this is specific to n8n's sandbox, not a plain Node.js process. Rewritten as a dependency-free regex (see [Link security design](#link-security-design)); re-verified against 16 unit cases and rows 37–38 live | real n8n execution (debug workflow), unit test (16 cases) |
| 40 | Inclusive expiry boundary (`valid_until <= now()`): a quotation seeded with `valid_until = now()` at insert time | `expired` (previously would have reported `delivery_pending`/reached the sender under the old exclusive `<` boundary) | real n8n execution |
| 41 | Full regression sweep after all fixes: happy-path send, draft/cancelled/rejected/accepted/expired/missing/conflict, oversized-id and missing-field input validation, sender HTTP 400/500/malformed-2xx/timeout, sequential duplicate replay, concurrent idempotency-key race (`BEGIN`/`pg_sleep` technique against the fixed schema), idempotency mismatch | Every case: identical, correct behavior to the original 29-scenario suite — no regression introduced by the graph restructuring (`Load Configuration`/`Configuration Valid?` insertion) or the schema changes. Mock call count matched the exact number of legitimate sends across the whole regression run | mock-bound copy, real n8n execution, direct SQL |
| 42 | Portability re-verification: official CLI export of the corrected workflow, content-diffed field-by-field (`nodes`, `connections`, `settings`) against the hand-built corrected draft | Byte-for-byte identical | real committed file |

### Corrections retest — second external adversarial review

A follow-up round after all three findings in [Corrections applied after a second external adversarial review](#corrections-applied-after-a-second-external-adversarial-review) were fixed. Run against a fresh Postgres instance with the corrected schema, and a fresh n8n test instance with the corrected workflow re-imported.

| # | Test | Result | Verified via |
|---|---|---|---|
| 43 | **Finding 1** — `valid_until = 'infinity'`, reservation attempted (via a temporary constraint bypass, since `quotations_valid_until_finite_check` itself already blocks this at `INSERT` time under normal operation) | `owner_applied`/`invalid_database_state`, zero `quotation_delivery_state` rows created | direct SQL |
| 44 | `valid_until = '-infinity'`, same technique | `owner_applied`/`invalid_database_state` (not `expired` — the isfinite check runs before the expiry comparison so both directions are caught uniformly), zero `quotation_delivery_state` rows | direct SQL |
| 45 | Regression: a normal past-dated `valid_until`, and the existing `valid_until = now()` inclusive boundary | Both unaffected: `expired` in both cases | direct SQL |
| 46 | **Finding 1 live** — `valid_until = 'infinity'` through the real n8n graph (mock-bound copy, real configuration) | `invalid_database_state`, mock call count unchanged (zero sends) | real n8n execution |
| 47 | **Finding 2** — `finalize_quotation_delivery` called with the new `'invalid_database_state'` outcome after a real `delivery_pending` reservation | `quotation_delivery_state.status` moves to `invalid_database_state`, `provider_message_id` stays `NULL`, `quotation_delivery_events.result_status` recorded the same | direct SQL |
| 48 | `'invalid_database_state'` outcome + non-null `provider_message_id` | Rejected (`finalize_invalid_provider_message_id`), zero mutation, state remains `delivery_pending` | direct SQL |
| 49 | Fresh-key retry against a quotation already in `invalid_database_state` | Blocked — `invalid_database_state` returned again, the existing state row's `active_idempotency_key` left untouched, matching `reconciliation_required`'s reopening protection | direct SQL |
| 50 | **Finding 2 live** — a genuine, temporary test-only mismatch between database acceptance and n8n validation: `process_quotation_delivery` temporarily replaced with a variant omitting its own `link_token` re-validation, a quotation with a corrupted `link_token` reserved, then delivered through the real mock-bound graph | Before: `delivery_pending` reserved despite the corrupt data (confirming the mismatch condition). After the live execution: `quotation_delivery_state.status` = `invalid_database_state`, `quotation_delivery_events.result_status` = `invalid_database_state`, zero mock sender calls (call count unchanged), and a fresh-key retry immediately afterward was correctly blocked rather than silently resending (mock call count still unchanged) | real n8n execution against the mock-bound copy |
| 51 | Restoration check: the real, fully-validating `process_quotation_delivery` reinstalled immediately after row 50, reproducing the original reservation-time rejection | `owner_applied`/`invalid_database_state` at reservation time again, zero `quotation_delivery_state` rows — confirms the test-only variant left no lasting effect | direct SQL |
| 52 | **Finding 3** — configuration validator: valid production-shaped configuration; every shipped placeholder; malformed `GRAPH_API_VERSION`/`PHONE_NUMBER_ID`/`TEMPLATE_NAME`/`LANGUAGE_CODE`; leading-dot/trailing-dot/consecutive-dot host; leading/trailing-hyphen DNS label; `example.com`/`.org`/`.net` and genuine subdomains of them; the `quotesexample.net` lookalike (must NOT be blocked); `localhost`; IPv4 and IPv6 literals; userinfo, port, query, fragment, and backslash-confusable origins; non-HTTPS origin; valid multi-label HTTPS origin with single- and multi-segment paths (39 cases total) | All 39 cases match expected — every malformed/placeholder/deceptive value rejected, every genuinely valid value and the lookalike domain accepted | unit test (39 cases), run against the exact generated node code (not just the isolated function) |
| 53 | **Finding 3 live** — shipped placeholder configuration against the real committed workflow; a mock-bound copy with a real, non-placeholder configuration | Shipped: `configuration_required`, zero Postgres/sender calls. Real config: genuine successful send (`sent`, HTTP 200, a real mock provider message id) | real n8n execution |
| 54 | Full regression sweep after all three fixes: happy-path send, draft/cancelled/expired quotations, sender HTTP 400, missing quotation | Every case: identical, correct behavior to the prior 42-scenario suite — no regression introduced by the new `Finalize Invalid Send Request` node, the restructured `Load Configuration`, or the schema changes | mock-bound copy, real n8n execution |
| 55 | Portability re-verification: official CLI export of the doubly-corrected workflow, content-diffed field-by-field (`nodes`, `connections`, `settings`) against the hand-built corrected draft, then re-imported into a **second**, fully independent clean n8n instance + fresh Postgres database | Byte-for-byte identical on both counts. Sender-by-id reference confirmed to resolve without manual rebinding on the second instance: a live execution completed successfully (`status: "success"`) and reached the expected `configuration_required` controlled response rather than an Execute Workflow "workflow not found" error | real committed file, second fully independent clean instance |

### Corrections retest — third external adversarial review

A follow-up round after both findings in [Corrections applied after a third external adversarial review](#corrections-applied-after-a-third-external-adversarial-review) were fixed. The 13 mandatory focused cases were run **inside a real n8n Code node** via `n8n execute` against a debug workflow built from the exact generated node code (not a hand-retyped copy), specifically because an earlier round's `URL`-global defect already proved plain Node.js execution alone is not sufficient to catch every n8n-sandbox-specific behavior.

| # | Test | Result | Verified via |
|---|---|---|---|
| 56 | `'00000'` (5-zero phone id) | `configuration_required` (rejected — all-zero, regardless of length) | real n8n Code-node execution (`n8n execute`) |
| 57 | `'000000000000000'` (15 zeroes, the original placeholder) | `configuration_required` (rejected) | real n8n Code-node execution |
| 58 | `'00000000000000000000'` (20 zeroes) | `configuration_required` (rejected) | real n8n Code-node execution |
| 59 | Valid non-zero 5–20-digit phone id (`'15511111111'`) | Accepted | real n8n Code-node execution |
| 60 | `127.0.0.1` (canonical IPv4) | Rejected | real n8n Code-node execution |
| 61 | `127.1` (shortened/integer-like IPv4-shaped, 2 labels) | Rejected | real n8n Code-node execution |
| 62 | `11111111111.1` (integer-like 2-label all-numeric host) | Rejected | real n8n Code-node execution |
| 63 | `999.999.999.999` (out-of-range 4-label numeric) | Rejected | real n8n Code-node execution |
| 64 | `123.quotes.company.com` (numeric subdomain, host as a whole not all-numeric) | Accepted | real n8n Code-node execution |
| 65 | `1.2.3.com` (multiple numeric-shaped labels, non-numeric final label, host not all-numeric) | Accepted | real n8n Code-node execution |
| 66 | `/./` path (dot segment, otherwise-valid host) | Rejected | real n8n Code-node execution |
| 67 | `/../` path (dot-dot segment, otherwise-valid host) | Rejected | real n8n Code-node execution |
| 68 | Normal multi-label HTTPS hostname with a safe multi-segment path (`/a/b/`) | Accepted | real n8n Code-node execution |
| 69 | Full regression: shipped placeholder configuration against the real committed workflow; a mock-bound copy with a real, non-placeholder configuration; draft/expired quotations | Shipped: `configuration_required`, zero Postgres/sender calls. Real config: genuine successful send. draft/expired: unaffected, correct labels | real n8n execution |
| 70 | Portability re-verification: official CLI export content-diffed field-by-field (`nodes`, `connections`, `settings`) against the hand-built corrected draft, then re-imported into the same second, independently clean n8n instance used in row 55 | Byte-for-byte identical on both counts | real committed file, second fully independent clean instance |

The committed JSON was produced by an official CLI export, then had n8n's own instance-specific metadata fields (`active`, `createdAt`, `updatedAt`, `versionId`, `triggerCount`, and similar) stripped to match every other workflow package in this repository's identical committed shape (`connections`, `id`, `meta`, `name`, `nodes`, `pinData`, `settings`, `staticData`, `tags`) — confirmed field-by-field that this stripping changed nothing about the nodes, connections, or settings themselves.

All test data was synthetic: fake quotation/phone/idempotency-key/link-token identifiers, a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, and a local mock server — no real Postgres database, WhatsApp/Meta credentials, or customer data anywhere.

## Known limitations

- **Does not create, price, or approve quotations.** `quotations` must already be populated, priced, and approved by whatever system owns real quotation data — including generating a genuinely unguessable `link_token` per quotation.
- **Does not generate a PDF or any rendered quotation document.** The quotation's actual content lives at the linked page, hosted entirely by the quotation-owning business's own application — this workflow only delivers a validated link to it.
- **"Sent" means the WhatsApp API accepted the message — not delivered, read, or acted on.** See [Exact scope](#exact-scope).
- **Assumes a 2-decimal currency exponent.** `total_minor_units` formatting divides by 100 (via string slicing, not floating-point arithmetic) for every currency — this is correct for EUR/USD/GBP and most ISO 4217 currencies, but incorrect for 0-decimal currencies (e.g. JPY) or 3-decimal currencies (e.g. BHD, KWD) without extension.
- **Does not decide which quotation is worth delivering, or when.** That decision is entirely outside this workflow's scope — it only verifies eligibility for and applies a single already-decided delivery attempt.
- **A stuck `delivery_pending` or `reconciliation_required` row requires manual or separate-workflow resolution.** This workflow deliberately never automatically retries a send for a pending or unresolved delivery, because a prior request may already have reached WhatsApp. A stuck `invalid_database_state` row is different — zero sender calls ever occurred for it — but still requires manual correction of the underlying quotation's data before any further attempt; see [Crash and reconciliation behavior](#crash-and-reconciliation-behavior).
- **A compromised Postgres or WhatsApp sender credential defeats this workflow's own guarantees entirely** — the optimistic-concurrency and idempotency mechanisms protect against races and duplicate processing, not against a credential that shouldn't have been trusted in the first place.
- **Every shipped configuration placeholder, and every malformed configuration value, is rejected by `Load Configuration` until replaced with a correctly formatted real value** — see [Setup steps](#setup-steps), [Link security design](#link-security-design), and [Corrections applied after a second external adversarial review](#corrections-applied-after-a-second-external-adversarial-review). This still cannot detect a real-looking, correctly formatted, but *substantively* wrong configuration (e.g. the right format but the wrong business's phone number id, or a genuine but unintended hostname) — format and known-placeholder validation is not the same as verifying the value is actually correct for your deployment.
- **No automatic retries, intentionally, anywhere.**
- **Execution access through the n8n API is disabled and the execution is soft-deleted immediately.** Physical removal is handled separately by n8n's pruning configuration — see [Test procedure](#test-procedure) for exactly what was and wasn't directly observed in this workflow's own testing.
- **The real Meta/WhatsApp API was never contacted during testing** — send behavior was verified only through a temporary, uncommitted mock-bound copy, disclosed precisely above. Verify against your own real, non-production WhatsApp Business setup before relying on this.
- This workflow has been verified as a template against the specific n8n, Node.js, and PostgreSQL versions documented here. It is **not** described as production-ready or production-tested.
- Only n8n core nodes are used; this has not been tested against any n8n Enterprise-only feature, and none are required.

## Regional and legal limitation

**Tax rules, quotation validity requirements, and what makes a quotation legally binding are inherently local and vary by jurisdiction.** This workflow does not calculate, verify, or claim any authority over tax, discount, total correctness, or legal bindingness — all of that is the sole responsibility of the quotation-owning business/application that created and approved the quotation before this workflow was ever called. Consult your own regional compliance requirements (see this repository's [Global scope and regional resources](../README.md#global-scope-and-regional-resources) for examples) before relying on this workflow as any part of a compliant quotation process.

## Data handled

Reads `idempotencyKey`, `quotationId`, and `expectedVersion` from its caller — **not** a recipient phone, quotation number, amount, currency, valid-until date, or link, all of which are read exclusively from Postgres. Reads and writes `quotations` (read-only — this workflow never changes a quotation's own status/amount/recipient/valid-until date/link token), `quotation_delivery_state`, and `quotation_delivery_events`. Makes at most one outbound call — to the existing sender sub-workflow, which itself makes at most one outbound HTTP call to the fixed WhatsApp Business Cloud API host — only after Postgres durably records `delivery_pending`. Its controlled output contains only `status`, `quotationId`, `httpStatus`, and `providerMessageId` — never the recipient phone, quotation contents, amount, the quotation link or its token, template parameters, database details, credentials, or raw provider responses.

## License and source

CC0-1.0 (see [`LICENSE`](../LICENSE)). Original workflow, built for this repository — not adapted from a third-party template.

## Last verification date

2026-08-25
