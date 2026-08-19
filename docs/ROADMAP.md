# Workflow Roadmap

This tracks workflow packages for this repository — both shipped and planned. Planned entries have no `.json` export or documentation yet, and no release dates are given because none have been set.

A workflow moves out of the planned list and into [`workflows/`](../workflows/README.md) only after it has been sanitized, documented per the [Workflow quality requirements](../README.md#workflow-quality-requirements), successfully re-imported into a clean n8n instance, and passed [automated validation](../README.md#how-validation-works).

## Available

| Workflow | Files | Notes |
|---|---|---|
| WhatsApp inbound support router | [`.json`](../workflows/whatsapp-inbound-support-router.json) · [`.md`](../workflows/whatsapp-inbound-support-router.md) | Deterministic keyword routing into appointments/billing/support/general. Sanitized, tested, re-import verified. Not production-ready — see its documented limitations. |

## Planned workflows

| Workflow | Status | Notes |
|---|---|---|
| WhatsApp appointment reminder | Planned — not available yet | Sends a reminder ahead of a scheduled appointment. |
| WhatsApp appointment confirmation and cancellation | Planned — not available yet | Handles the confirm/cancel reply flow for a booking. |
| Google Calendar ↔ Postgres synchronization | Planned — not available yet | Keeps appointment records and calendar events in sync. |
| Unpaid invoice reminder | Planned — not available yet | Automated follow-up for overdue invoices. |
| Customer quotation delivery | Planned — not available yet | Generates and delivers a quotation to a customer. |
| n8n workflow execution through MCP | Planned — not available yet | Triggers/monitors an n8n workflow from an MCP-connected client. |

## How to propose a new planned workflow

Open a pull request adding a row to the table above with a one-line description, or open an issue describing the business use case. Proposing a workflow here does not require having built it yet — but publishing it in `workflows/` does require a real, sanitized, tested export (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)).
