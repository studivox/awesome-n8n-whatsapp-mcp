# Workflow Roadmap

This tracks workflow packages for this repository — both shipped and planned. Planned entries have no `.json` export or documentation yet, and no release dates are given because none have been set.

A workflow moves out of the planned list and into [`workflows/`](../workflows/README.md) only after it has been sanitized, documented per the [Workflow quality requirements](../README.md#workflow-quality-requirements), successfully re-imported into a clean n8n instance, and passed [automated validation](../README.md#how-validation-works).

## Available

| Workflow | Files | Notes |
|---|---|---|
| WhatsApp inbound support router | [`.json`](../workflows/whatsapp-inbound-support-router.json) · [`.md`](../workflows/whatsapp-inbound-support-router.md) | Deterministic keyword routing into appointments/billing/support/general. Sanitized, tested, re-import verified. Not production-ready — see its documented limitations. |
| WhatsApp appointment reply parser | [`.json`](../workflows/whatsapp-appointment-reply-parser.json) · [`.md`](../workflows/whatsapp-appointment-reply-parser.md) | Classifies a customer's free-text reply as confirmed/cancelled/reschedule_requested/manual_review. A **building block, not the full automation** — it does not touch a calendar or database, and does not send any message. Sanitized, tested, re-import verified. Not production-ready. |
| WhatsApp template message sender | [`.json`](../workflows/whatsapp-template-message-sender.json) · [`.md`](../workflows/whatsapp-template-message-sender.md) | Reusable sub-workflow (Execute Workflow Trigger) that sends one approved WhatsApp template message and returns controlled delivery metadata. A **building block**, not a scheduler or reminder system on its own — other planned workflows below (appointment reminder, invoice reminder, quotation delivery) would call this rather than duplicate the send logic. Tested against a local mock server only, never live Meta. Sanitized, re-import verified. Not production-ready. |
| WhatsApp delivery status parser | [`.json`](../workflows/whatsapp-delivery-status-parser.json) · [`.md`](../workflows/whatsapp-delivery-status-parser.md) | Parses one WhatsApp delivery-status webhook event (sent/delivered/read/failed/unknown future values) into a safe 5-field summary. A **building block** — stores nothing, sends nothing, and is not an end-to-end delivery-tracking system on its own. Sanitized, tested, re-import verified. Not production-ready. |
| WhatsApp webhook security gateway | [`.json`](../workflows/whatsapp-webhook-security-gateway.json) · [`.md`](../workflows/whatsapp-webhook-security-gateway.md) | Verifies Meta's GET handshake and POST `X-Hub-Signature-256` signature (over the exact raw body) before anything downstream runs. Both secrets live only in n8n Crypto credentials, never in the exported JSON. Ships with execution-data persistence disabled by workflow settings (verified experimentally; physical deletion is bounded, not instant) and an unmistakable placeholder commitment that fails closed until replaced. A **building block** — meant to sit in front of the other webhook-based workflows above. 26-test suite, re-import verified. Not a certified production security product, not a claim of replay protection, and does not control upstream infrastructure logging — see its documentation's [Infrastructure logging risk](../workflows/whatsapp-webhook-security-gateway.md#infrastructure-logging-risk) section. |

## Planned workflows

| Workflow | Status | Notes |
|---|---|---|
| WhatsApp appointment reminder | Planned — not available yet | Sends a reminder ahead of a scheduled appointment. Would call the available "WhatsApp template message sender" rather than reimplement the send step. |
| WhatsApp appointment confirmation and cancellation | Planned — not available yet | The full calendar/database-updating automation. The available "WhatsApp appointment reply parser" above only classifies the customer's reply text — this planned item is the end-to-end automation that would act on that classification. |
| Google Calendar ↔ Postgres synchronization | Planned — not available yet | Keeps appointment records and calendar events in sync. |
| Unpaid invoice reminder | Planned — not available yet | Automated follow-up for overdue invoices. |
| Customer quotation delivery | Planned — not available yet | Generates and delivers a quotation to a customer. |
| n8n workflow execution through MCP | Planned — not available yet | Triggers/monitors an n8n workflow from an MCP-connected client. |

## How to propose a new planned workflow

Open a pull request adding a row to the table above with a one-line description, or open an issue describing the business use case. Proposing a workflow here does not require having built it yet — but publishing it in `workflows/` does require a real, sanitized, tested export (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)).
