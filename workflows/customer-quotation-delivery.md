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
- **Code** (`n8n-nodes-base.code`, v2) — used eight times: input validation, reserve-result classification, the status-response builder, the send-request builder (link/money validation and formatting), the invalid-send-request-result builder, send-result classification, and the final finalize-response builder — plus the rejected-input response builder.
- **IF** (`n8n-nodes-base.if`, v2.3) — used three times: input validity, whether a send is actually needed, and whether the built send request (link token, formatted amount) itself passed validation before ever reaching the sender.
- **Postgres** (`n8n-nodes-base.postgres`, v2.6) — used twice (`Reserve And Apply`, `Finalize Send Result`), `Execute Query` operation, every query fully parameterized (`$1, $2, ...` placeholders with a separate values array — never string-built SQL). See [Atomic idempotency and ownership design](#atomic-idempotency-and-ownership-design).
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

## Link security design

**Option A was chosen: a fixed, hardcoded HTTPS origin plus a strictly validated opaque token — never a full URL read from Postgres or the caller.**

```js
const QUOTATION_LINK_ORIGIN = 'https://quotes.example.com/q/'; // replace before real use

function isValidLinkToken(v) {
  return typeof v === 'string' && v.length >= 16 && v.length <= 128 && /^[A-Za-z0-9_-]+$/.test(v);
}

const quotationLink = QUOTATION_LINK_ORIGIN + encodeURIComponent(c.linkToken);
```

`link_token` is bounded to a strict alphanumeric-plus-underscore-plus-hyphen character set at the database layer (`quotations_link_token_check`) and re-validated identically in `Build Send Request` before ever being used to build a URL — defense in depth, not reliance on the database constraint alone. This character set structurally **cannot** contain a URL scheme (`http://`), a different host, userinfo (`user:pass@`), a port, or a path-traversal sequence (`../`) — there is nothing to allowlist against because none of those characters are ever valid in the first place. Verified directly: a URL-shaped, userinfo-shaped, and path-traversal-shaped token were each rejected by the database `CHECK` constraint at `INSERT` time; a URL-shaped token that bypassed the constraint (dropped temporarily for this test) was still rejected by the n8n-layer validation, with zero calls reaching the sender.

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
  CONSTRAINT quotations_version_check CHECK (version >= 1 AND version <= 1000000000)
);

CREATE TABLE quotation_delivery_state (
  quotation_id            TEXT PRIMARY KEY,
  status                  TEXT NOT NULL,   -- 'delivery_pending' | 'sent' | 'failed' | 'reconciliation_required'
  active_idempotency_key  TEXT,             -- the key currently owning this quotation's pending/terminal attempt
  reserved_version        INTEGER,          -- the quotation version observed when active_idempotency_key reserved
  provider_message_id     TEXT,
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT qds_quotation_id_check CHECK (quotation_id ~ '^[A-Za-z0-9_-]{1,128}$'),
  CONSTRAINT qds_status_check CHECK (status IN ('delivery_pending', 'sent', 'failed', 'reconciliation_required')),
  CONSTRAINT qds_active_key_check CHECK (active_idempotency_key IS NULL OR active_idempotency_key ~ '^[A-Za-z0-9_-]{1,128}$'),
  CONSTRAINT qds_reserved_version_check CHECK (reserved_version IS NULL OR (reserved_version >= 1 AND reserved_version <= 1000000000)),
  CONSTRAINT qds_provider_message_id_check CHECK (provider_message_id IS NULL OR (length(provider_message_id) BETWEEN 1 AND 256 AND provider_message_id ~ '^[A-Za-z0-9_.=-]+$'))
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
```

`quotation_delivery_state` has **no composite key** (unlike `unpaid-invoice-reminder`'s per-stage state table) — a quotation is delivered once, not through multiple independent stages, so `quotation_id` alone is the correct primary key here. Deliberately not copied blindly from the sibling workflow's shape.

**The reservation function**, modeled directly on `process_reply_event`'s proven exception-safe ownership-gate pattern and the corrected `process_invoice_reminder`:

```sql
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
    -- See Known issue found and fixed during this build below.
    IF v_quote.status IN ('draft', 'cancelled', 'rejected', 'accepted') THEN
      UPDATE quotation_delivery_events SET result_status = v_quote.status WHERE idempotency_key = p_idempotency_key;
      RETURN QUERY SELECT 'owner_applied'::TEXT, v_quote.status, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    ELSE
      UPDATE quotation_delivery_events SET result_status = 'invalid_database_state' WHERE idempotency_key = p_idempotency_key;
      RETURN QUERY SELECT 'owner_applied'::TEXT, 'invalid_database_state'::TEXT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, v_quote.version;
    END IF;
    RETURN;
  END IF;

  IF v_quote.valid_until < now() THEN
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
```

**Why the reservation `INSERT` (not a same-snapshot check) is the actual safety mechanism:** Postgres's unique index on `idempotency_key` guarantees exactly one concurrent caller can ever complete that `INSERT` — independent of statement snapshot timing. The loser's `EXCEPTION WHEN unique_violation` handler runs only *after* Postgres resolves the conflict against the real, committed winning row, so the loser's read of the existing row is never a stale pre-commit snapshot. Verified directly: two concurrent processes racing the same `idempotencyKey` (one held open across the statement via `BEGIN; ...; pg_sleep(...); COMMIT;`, the other executing concurrently) produce exactly one `owner_applied` and one `duplicate_match` reporting the winner's real, correct status — never `NULL`, never stale.

**`already_delivered` vs. `delivery_pending` are reported as two distinct outcomes**, not collapsed into one label: a quotation whose delivery already succeeded (`status = 'sent'`) reports `already_delivered`; a quotation whose delivery is genuinely still in flight (a different, concurrent attempt owns it) reports `delivery_pending` — a caller can tell the difference between "this already went out" and "someone else is sending this right now" without querying Postgres directly.

**Finalize (ownership-verified, and version-guarded using only the durably stored reservation, never a caller-supplied value):**

```sql
CREATE OR REPLACE FUNCTION finalize_quotation_delivery(
  p_idempotency_key      TEXT,
  p_quotation_id         TEXT,
  p_outcome              TEXT,   -- 'sent' | 'failed'
  p_provider_message_id  TEXT
) RETURNS TABLE (out_ok BOOLEAN, out_result_status TEXT) AS $$
DECLARE
  v_event quotation_delivery_events%ROWTYPE;
  v_state quotation_delivery_state%ROWTYPE;
  v_current_version INTEGER;
  v_new_status TEXT;
BEGIN
  IF p_outcome NOT IN ('sent', 'failed') THEN
    RETURN QUERY SELECT false, 'finalize_invalid_outcome'::TEXT;
    RETURN;
  END IF;

  v_new_status := CASE WHEN p_outcome = 'sent' THEN 'sent' ELSE 'failed' END;

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

  SELECT version INTO v_current_version FROM quotations WHERE quotation_id = p_quotation_id;

  IF v_current_version IS DISTINCT FROM v_state.reserved_version THEN
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

There is **no `p_expected_version` parameter** — the exact same correction already applied to `unpaid-invoice-reminder`'s `finalize_invoice_reminder` after a real ownership-verification defect was found there (see that workflow's own [Finalize-ownership correction](unpaid-invoice-reminder.md#finalize-ownership-correction)) is designed in from the start here: `finalize_quotation_delivery` locks and verifies the exact `quotation_delivery_events` row for the supplied `idempotencyKey`, requires it to exist and match the supplied `quotationId`, requires its `result_status` to still be `delivery_pending`, then locks `quotation_delivery_state` and requires `active_idempotency_key` to match the same key **and** `status` to still be `delivery_pending` — proof that this exact attempt, not a different or newer one, currently owns the row. Only then does it check the quotation's current version against the **durably stored** `reserved_version`. A nonexistent key, a mismatched `quotationId`, or an invalid `p_outcome` value mutates nothing and returns a controlled failure (`finalize_unknown_key` / `finalize_mismatch` / `finalize_invalid_outcome`). A replay against an already-terminal event returns that event's real, existing result unchanged — it can never flip `sent` to `failed`, `failed` to `sent`, or either into `reconciliation_required`.

Verified directly against Postgres, all reproduced against the real committed graph, not just the raw functions: a finalize call with a nonexistent key, with another real attempt's key (confirmed: attempting to hijack a *different*, genuinely in-flight quotation's delivery with a real key belonging to a different quotation — zero mutation on either row), with the correct key but a wrong quotation, and with an invalid outcome value all mutate nothing. A replay against an already-`sent` attempt trying to flip it to `failed` (and the reverse, against an already-`failed` attempt) leaves the state and `provider_message_id` completely unchanged. A stale attempt finalizing after a fresh reservation has taken over the same `quotation_id` (simulating a manual reconciliation freeing it) is rejected with the newer owner's row left byte-for-byte untouched. See [Test procedure](#test-procedure) for the complete adversarial suite.

## Known issue found and fixed during this build

**What was wrong:** the first version of `process_quotation_delivery` wrote `v_quote.status` directly into `quotation_delivery_events.result_status` whenever a quotation wasn't `approved`, trusting `quotations_status_check` as the only thing bounding that value to a known set. If `quotations_status_check` were ever bypassed at the database level (e.g. an administrator dropping the constraint, or a future migration mistake) and an out-of-enum `status` value existed on a row, that `UPDATE` would violate `quotation_delivery_events`'s own **separate, narrower** `qde_result_status_check` constraint and abort the entire statement with a hard Postgres error — surfacing as an n8n execution failure rather than a controlled `invalid_database_state` response. Found empirically, not by inspection alone: reproduced directly by temporarily dropping `quotations_status_check`, inserting a row with `status = 'totally_bogus_status'`, and calling `process_quotation_delivery` against it — confirmed the hard error, including through the real committed n8n graph (top-level execution `status: "error"`).

**The fix:** `v_quote.status` is now explicitly validated against the exact known set (`draft`/`cancelled`/`rejected`/`accepted`) before ever being written anywhere. An unrecognized value degrades to the explicit, honest `invalid_database_state` — now a formally allowed value in `qde_result_status_check` — instead of crashing. The corresponding n8n-layer allowlists (`Classify Reserve Result` and `Build Finalize Response`) were also updated to recognize `invalid_database_state` as a legitimate passthrough value rather than relying on it merely coinciding with their own fallback literal. Re-verified: the identical reproduction (bypassed constraint, bogus status, reservation attempt) now returns `owner_applied`/`invalid_database_state` at the SQL layer and a clean `{"status":"invalid_database_state",...}` controlled response through the real n8n graph, with the top-level execution completing successfully rather than erroring. Full regression (draft/cancelled/approved paths) re-confirmed unaffected.

This gap is not externally reachable — `p_quotation_id`/`p_idempotency_key`/`p_expected_version` are the only caller-influenced inputs, and none of them can produce an out-of-enum `quotations.status`; reaching this path requires the database's own primary constraint to already be bypassed. It was still worth closing, both because "unknown database values must fail closed" is a requirement of this workflow's own design, not only a defense against a hostile caller, and because a hard execution error is a strictly worse operator experience than a controlled, understandable response.

## Sender sub-workflow binding

`Call Sender` is an Execute Workflow node with `source: "database"` and `workflowId: {"mode": "id", "value": "R1QDUW9jYqxREyDS", "cachedResultName": "WhatsApp Template Message Sender"}` — the exact same reference mechanism [`whatsapp-appointment-reminder`](whatsapp-appointment-reminder.md) and [`unpaid-invoice-reminder`](unpaid-invoice-reminder.md) already established and documented in detail. Re-verified directly for this workflow: imported the real, unmodified sender alongside this workflow into a fresh instance and confirmed the reference resolves without manual rebinding, and re-confirmed the same on a second, independently clean instance after a full CLI export/import round trip. See [Clean re-import results](#clean-re-import-results). **If you re-export or otherwise change the sender workflow in a way that changes its `id`,** this reference will break explicitly (Execute Workflow surfaces a clear error, it does not fail open) — update `Call Sender`'s `workflowId.value` to match.

## Sender-output fail-closed design

`Classify Send Result` never trusts the sender's `status` field alone. The sender's own controlled contract (`{status, httpStatus, providerMessageId}`) can legitimately return `status: 'sent'` with `providerMessageId: null` for a malformed 2xx provider response (its own documented behavior — see [`whatsapp-template-message-sender.md`](whatsapp-template-message-sender.md)). A send is only ever treated as successful when **all three** fields agree: `status` is exactly `'sent'`, `httpStatus` is a genuine 2xx integer, and `providerMessageId` is a non-empty, bounded (1–256 character), safely-charactered identifier (`^[A-Za-z0-9_.=-]+$`) — the same policy `unpaid-invoice-reminder` already established for this repository, reused here rather than reinvented. Anything short of all three becomes `failed`; a non-`sent` outcome never retains a `providerMessageId`. Verified directly against the real graph: the sender's documented malformed-2xx behavior correctly produces `failed` with `providerMessageId: null`, while a genuinely valid send is unaffected.

## Crash and reconciliation behavior

The complete state machine this workflow can leave a quotation in:

| State | Meaning |
|---|---|
| *(no `quotation_delivery_events` row)* | This `idempotencyKey` was never durably reserved — either rejected before the atomic write, or the atomic write never ran (e.g. Postgres was unavailable). |
| `missing_quotation` | The atomic write ran; no quotation with this id exists. |
| `conflict` | The atomic write ran; `expectedVersion` did not match the quotation's current version. |
| `draft` / `cancelled` / `rejected` / `accepted` | The atomic write ran; the quotation's real, current status makes delivery inappropriate. |
| `expired` | The atomic write ran; the quotation's valid-until date has passed (checked against Postgres's own clock). |
| `already_delivered` | The atomic write ran; this quotation was already successfully delivered. |
| `delivery_pending` | The atomic write committed this durable pending marker before any WhatsApp call was attempted — reported both as the reservation owner's own in-flight state and to any other concurrent/duplicate request. This is the only state a crash between the database write and the send call (or between the send call and finalizing its result) can leave visible. |
| `sent` | The send succeeded and was finalized via the ownership-verified, version-guarded write — full success. |
| `failed` | The send was attempted and failed (any non-2xx status, a transport failure, or an internally-inconsistent malformed `sent` response), finalized via the ownership-verified write. Retriable via a fresh `idempotencyKey`. |
| `reconciliation_required` | Either the finalize call's ownership check failed (the event row no longer agreed with the state row it should own), or the quotation moved on since this attempt's reservation. In both cases the row that failed ownership/version verification is left completely untouched; a human or a separate reconciliation process must resolve this by hand. |

Two further outcomes (`finalize_unknown_key`, `finalize_mismatch`) and one input-validation outcome (`finalize_invalid_outcome`) exist only as defense-in-depth responses from `finalize_quotation_delivery` itself against a finalize call with a nonexistent `idempotencyKey`, one that belongs to a genuinely different `quotationId`, or a garbage `outcome` value — none of these are reachable through this workflow's own committed graph under normal operation, but the function refuses them regardless, mutating nothing, since it is also a general-purpose database function another caller could invoke directly.

**If the process crashes after the atomic database write commits but before or during the send call**, the database durably and visibly shows `delivery_pending`. A duplicate request for the same `idempotencyKey` reports this exact pending state and makes zero new sends. A fresh `idempotencyKey` for the same `quotationId` is rejected as `delivery_pending` (if still genuinely in flight) or `already_delivered` (if it has since completed) — this workflow never allows a second overlapping attempt at the same quotation regardless of which idempotency key is used.

**If the send itself succeeds or fails but the final Postgres write then fails** (e.g. Postgres becomes unreachable at exactly that moment), this workflow does **not** automatically retry the send — the message may already have reached WhatsApp, and retrying blindly risks a duplicate. The execution fails loudly (no `continueOnFail` on the finalize node), and the delivery state is left showing `delivery_pending` — its state from the initial atomic write — until a human or a separate reconciliation process checks WhatsApp's actual delivery state and updates Postgres by hand. Verified directly by isolating the finalize write against a deliberately unreachable Postgres connection after a successful mock send: exactly one send occurred, and the state remained durably `delivery_pending` — never falsely marked `sent`.

**Resolving a stuck `delivery_pending` or `reconciliation_required` row is outside this workflow's scope.** It requires checking the actual delivery state on WhatsApp's side and manually updating `quotation_delivery_state` (and, if appropriate, `quotation_delivery_events.result_status`) — this workflow deliberately does not attempt that automatically.

## Controlled output contract

Every execution branch returns exactly the same four-field shape — proven by construction (every terminal Code node in this workflow builds this exact shape, nothing more) and by test: `status`, `quotationId`, `httpStatus`, `providerMessageId`. **Never** the recipient phone, quotation number, amount, currency, valid-until date, the quotation link or its token, template parameters, credentials, raw provider responses, or SQL/database errors. `httpStatus`/`providerMessageId` are `null` on every path that never reaches the sender (all rejection/ineligibility/duplicate paths, and the `Send Request Valid?` false branch).

## Required credentials

**One**, not included in the exported JSON — no node has a credential bound after import, by design:

| Credential | Bound to node(s) | Type |
|---|---|---|
| e.g. "Quotations Postgres" | `Reserve And Apply`, `Finalize Send Result` | n8n **Postgres** credential (`postgres`), pointed at your own database with the schema and functions in [Atomic idempotency and ownership design](#atomic-idempotency-and-ownership-design) |

The sender's own credential (its HTTP Header Auth WhatsApp access token) lives only in the already-committed [`whatsapp-template-message-sender`](whatsapp-template-message-sender.md) workflow — this workflow never touches it directly.

## Environment variables

**None.** No `$env`, no `$vars`, and no instance-level configuration change is required or used anywhere in this workflow.

## Setup steps

1. Create the schema — see [Atomic idempotency and ownership design](#atomic-idempotency-and-ownership-design) for a new installation.
2. Import `customer-quotation-delivery.json`.
3. Create and bind your Postgres credential (see [Required credentials](#required-credentials)) to `Reserve And Apply` and `Finalize Send Result`.
4. Ensure [`whatsapp-template-message-sender.json`](whatsapp-template-message-sender.json) is already imported (with its own WhatsApp credential bound) — if you imported it via the official CLI or the editor's normal import feature without changing its id, `Call Sender`'s reference resolves automatically.
5. **Edit `Build Send Request`'s hardcoded constants before real use**: `PHONE_NUMBER_ID` to your real WhatsApp Business phone number id, `TEMPLATE_NAME` to your own real, Meta-approved template name, and — critically — `QUOTATION_LINK_ORIGIN` to your own real, HTTPS quotation-viewing page's origin. This workflow ships with placeholder values (including an `example.com` link origin) that will not resolve to a real page or send successfully against the real API.
6. Populate `quotations` from your real quotation data however you already do that — this workflow does not create quotations, calculate their contents, or generate `link_token` values itself; your application must generate a genuinely unguessable, unique token per quotation and store it there.
7. Build whatever calls this sub-workflow — a sales action or automated job that decides *which* approved quotation is worth delivering right now, and calls this workflow once per candidate, passing a fresh `idempotencyKey` per attempt, the `quotationId`, and `expectedVersion` looked up from your own quotation records. **Do not pass a recipient phone, quotation values, or a link** — none of these are part of this workflow's input contract.
8. Have a plan for resolving `delivery_pending`/`reconciliation_required` rows that don't clear on their own — see [Crash and reconciliation behavior](#crash-and-reconciliation-behavior). This workflow does not do this automatically.
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

The committed JSON was produced by an official CLI export, then had n8n's own instance-specific metadata fields (`active`, `createdAt`, `updatedAt`, `versionId`, `triggerCount`, and similar) stripped to match every other workflow package in this repository's identical committed shape (`connections`, `id`, `meta`, `name`, `nodes`, `pinData`, `settings`, `staticData`, `tags`) — confirmed field-by-field that this stripping changed nothing about the nodes, connections, or settings themselves.

All test data was synthetic: fake quotation/phone/idempotency-key/link-token identifiers, a fake bearer token clearly labeled `SYNTHETIC_TEST_TOKEN`, and a local mock server — no real Postgres database, WhatsApp/Meta credentials, or customer data anywhere.

## Known limitations

- **Does not create, price, or approve quotations.** `quotations` must already be populated, priced, and approved by whatever system owns real quotation data — including generating a genuinely unguessable `link_token` per quotation.
- **Does not generate a PDF or any rendered quotation document.** The quotation's actual content lives at the linked page, hosted entirely by the quotation-owning business's own application — this workflow only delivers a validated link to it.
- **"Sent" means the WhatsApp API accepted the message — not delivered, read, or acted on.** See [Exact scope](#exact-scope).
- **Assumes a 2-decimal currency exponent.** `total_minor_units` formatting divides by 100 (via string slicing, not floating-point arithmetic) for every currency — this is correct for EUR/USD/GBP and most ISO 4217 currencies, but incorrect for 0-decimal currencies (e.g. JPY) or 3-decimal currencies (e.g. BHD, KWD) without extension.
- **Does not decide which quotation is worth delivering, or when.** That decision is entirely outside this workflow's scope — it only verifies eligibility for and applies a single already-decided delivery attempt.
- **A stuck `delivery_pending` or `reconciliation_required` row requires manual or separate-workflow resolution.** This workflow deliberately never automatically retries a send for a pending or unresolved delivery, because a prior request may already have reached WhatsApp.
- **A compromised Postgres or WhatsApp sender credential defeats this workflow's own guarantees entirely** — the optimistic-concurrency and idempotency mechanisms protect against races and duplicate processing, not against a credential that shouldn't have been trusted in the first place.
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
