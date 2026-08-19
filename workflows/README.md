# Workflows

This folder holds real, sanitized n8n workflow exports for WhatsApp Business Cloud API and related business-automation use cases (appointments, invoicing, customer support) for teams worldwide. Where a workflow depends on country-specific rules (e.g. invoicing compliance), that's called out explicitly in its own documentation as a regional variant — see [Global scope and regional resources](../README.md#global-scope-and-regional-resources).

**Status: no workflow files have been added yet.** This repository does not ship placeholder or fabricated workflow JSON — every entry here must be a real, working export that has actually been run. See the planned pipeline in [`docs/ROADMAP.md`](../docs/ROADMAP.md).

## Submission format

Each workflow must be submitted as a matching pair:

1. `workflow-slug.json` — the exact file exported from n8n (Workflow menu → **Download**), with no manual edits after export other than sanitization.
2. `workflow-slug.md` — documentation containing **all** of the following headings:

   - `## What it does`
   - `## Real business use case`
   - `## Required n8n version`
   - `## Required nodes`
   - `## Required credentials`
   - `## Environment variables`
   - `## Setup steps`
   - `## Test procedure`
   - `## Known limitations`
   - `## Data handled`
   - `## License and source`
   - `## Last verification date`

Credentials must be created manually inside n8n by whoever uses the workflow — never embedded in the JSON export, and never referenced by a live credential ID.

## Mandatory sanitization checklist

Before opening a PR, confirm the exported JSON contains **none** of the following:

- [ ] API keys, access tokens, or bearer tokens
- [ ] Real phone numbers (use placeholders like `+00000000000`)
- [ ] Customer names, emails, or any other personal data
- [ ] Real webhook URLs (use placeholders like `https://YOUR_N8N_INSTANCE/webhook/...`)
- [ ] n8n credential IDs bound to a live account (re-create credentials as empty placeholders referenced by name only)

`scripts/validate_repository.py` runs an automated pass for common secret patterns on every PR, but it is a defensive quality gate, not a guarantee — this manual checklist is still required.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the full contribution and review process.

## Example `.md` companion file

```markdown
# WhatsApp Appointment Reminder Workflow

## What it does
Sends an automatic WhatsApp Cloud API reminder message 24 hours before
an appointment, based on a customer record stored in Postgres and an
event read from Google Calendar.

## Real business use case
Reduces no-shows for appointment-based businesses without manual follow-up.

## Required n8n version
n8n v1.6x.x (fill in the exact tested version)

## Required nodes
- Schedule Trigger (runs hourly)
- Postgres (appointment lookup)
- Google Calendar (event details)
- HTTP Request (WhatsApp Cloud API, template message)

## Required credentials
- WhatsApp Cloud API access token (created manually in n8n)
- Postgres connection (created manually in n8n)
- Google Calendar OAuth2 (created manually in n8n)

## Environment variables
- `WHATSAPP_TOKEN` — placeholder, replace with your own
- `WHATSAPP_PHONE_ID` — placeholder, replace with your own

## Setup steps
1. Get the "appointment_reminder" template approved in WhatsApp Business Manager.
2. Add `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_ID` to your `.env`.
3. Add the Postgres connection to n8n credentials.
4. Import the workflow and configure the cron schedule.

## Test procedure
Ran against a synthetic Postgres appointment record and a test WhatsApp number;
confirmed the reminder was received with correct placeholders substituted.

## Known limitations
Does not handle timezone conversion across appointment and business locale.

## Data handled
Customer phone number and appointment time (from your own Postgres instance).

## License and source
CC0-1.0, original workflow.

## Last verification date
YYYY-MM-DD
```
