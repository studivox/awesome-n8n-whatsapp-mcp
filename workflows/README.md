# Workflows

This folder holds real, sanitized n8n workflow exports for WhatsApp Business Cloud API and related business-automation use cases (appointments, invoicing, customer support) for the Turkish and Dutch markets.

**Status: no workflow files have been added yet.** This list does not ship placeholder or fabricated workflow JSON — every entry here must be a real, working export that has actually been run.

## Submission format

Each workflow must be submitted as a matching pair:

1. `workflow-name.json` — the exact file exported from n8n (Workflow menu → **Download**), with no manual edits after export other than sanitization.
2. `workflow-name.md` — a short description covering:
   - What the workflow does
   - Required n8n nodes
   - Setup steps
   - Required environment variables / credential types (names only, never values)
   - **The n8n version it was tested against**

## Mandatory sanitization checklist

Before opening a PR, confirm the exported JSON contains **none** of the following:

- [ ] API keys, access tokens, or bearer tokens
- [ ] Real phone numbers (use placeholders like `+00000000000`)
- [ ] Customer names, emails, or any other personal data
- [ ] Real webhook URLs (use placeholders like `https://YOUR_N8N_INSTANCE/webhook/...`)
- [ ] n8n credential IDs bound to a live account (re-create credentials as empty placeholders referenced by name only)

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the full contribution and review process.

## Example `.md` companion file

```markdown
# WhatsApp Appointment Reminder Workflow

## What it does
Sends an automatic WhatsApp Cloud API reminder message 24 hours before
an appointment, based on a customer record stored in Postgres and an
event read from Google Calendar.

## Required nodes
- Schedule Trigger (runs hourly)
- Postgres (appointment lookup)
- Google Calendar (event details)
- HTTP Request (WhatsApp Cloud API, template message)

## Tested against
n8n v1.6x.x (fill in the exact tested version)

## Setup
1. Get the "appointment_reminder" template approved in WhatsApp Business Manager.
2. Add `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_ID` to your `.env`.
3. Add the Postgres connection to n8n credentials.
4. Import the workflow and configure the cron schedule.
```
