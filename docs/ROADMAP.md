# Workflow Roadmap

This tracks planned workflow packages for this repository. **Nothing in this document is available today** — every entry below is planned only, with no `.json` export or documentation published yet. No release dates are given because none have been set.

A workflow moves out of this list and into [`workflows/`](../workflows/README.md) only after it has been sanitized, documented per the [Workflow quality requirements](../README.md#workflow-quality-requirements), successfully re-imported into a clean n8n instance, and passed [automated validation](../README.md#how-validation-works).

## Planned workflows

| Workflow | Status | Notes |
|---|---|---|
| WhatsApp appointment reminder | Planned — not available yet | Sends a reminder ahead of a scheduled appointment. |
| WhatsApp appointment confirmation and cancellation | Planned — not available yet | Handles the confirm/cancel reply flow for a booking. |
| Incoming WhatsApp support routing | Planned — not available yet | Routes inbound messages to the right handler by keyword/intent. |
| Google Calendar ↔ Postgres synchronization | Planned — not available yet | Keeps appointment records and calendar events in sync. |
| Unpaid invoice reminder | Planned — not available yet | Automated follow-up for overdue invoices. |
| Customer quotation delivery | Planned — not available yet | Generates and delivers a quotation to a customer. |
| n8n workflow execution through MCP | Planned — not available yet | Triggers/monitors an n8n workflow from an MCP-connected client. |

## How to propose a new planned workflow

Open a pull request adding a row to the table above with a one-line description, or open an issue describing the business use case. Proposing a workflow here does not require having built it yet — but publishing it in `workflows/` does require a real, sanitized, tested export (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)).
