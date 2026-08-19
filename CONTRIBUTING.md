# Contributing Guide

Thanks for helping grow this list. Please read this before opening a PR.

## Adding a resource/project entry

1. Fork the repo.
2. Add a single line to the relevant section (e.g. `n8n — WhatsApp Cloud API`):
   ```
   | [Project Name](https://github.com/...) | Short description (max ~15 words) |
   ```
3. The link **must be a real, specific, clickable URL** you have personally opened and verified — not a project name without a link, not an organization page standing in for a repo, and not a "someone should build this" idea.
4. In your PR description, state:
   - What you tested and how.
   - Which n8n / Postgres / WhatsApp Cloud API version it was tested against (if applicable).
5. Check off the PR checklist below.

## What gets rejected

- Entries without a working, specific link.
- Self-promotional or unused "boilerplate" repos.
- Projects with no commits in the last 6+ months (unless it's a stable official reference, e.g. platform documentation).
- Resources already covered by general-purpose English "awesome n8n" lists that add no TR/NL business-automation context.
- Projects whose setup instructions don't actually work.
- Marketing language ("best", "guaranteed to work") instead of plain technical description.

## Submitting a workflow (`workflows/`)

If you're contributing your own n8n workflow, it must be a **real, working workflow you have actually run** — never a fabricated or hand-written JSON file that wasn't produced by n8n's own export.

### Required file pair

Every workflow submission needs **exactly two files** in `workflows/`:

1. `your-workflow-name.json` — exported directly from n8n (Workflow menu → **Download**). Do not hand-edit the JSON structure beyond the sanitization described below.
2. `your-workflow-name.md` — a companion description covering what it does, required nodes, setup steps, and required environment variables/credential names.

### You must declare the tested n8n version

Your `.md` file must state the exact n8n version (e.g. `n8n v1.6x.x`) the workflow was built and run against. PRs without a declared tested version will not be merged.

### Mandatory sanitization — no exceptions

Before exporting and committing, you must remove **all** of the following from the JSON:

- API keys, access tokens, bearer tokens
- Phone numbers (replace with placeholders, e.g. `+00000000000`)
- Customer names, emails, or any other personal/business data
- Real webhook URLs (replace with placeholders, e.g. `https://YOUR_N8N_INSTANCE/webhook/...`)
- n8n credential IDs bound to a live account — re-create referenced credentials as empty placeholders identified only by name/type, never by ID or stored value

### Verify before you open the PR

Before submitting, re-open your exported `.json` file and confirm:

- [ ] No credentials block contains real values (n8n normally excludes credential secrets from export, but always double-check any inline `parameters` fields too — tokens are sometimes pasted directly into HTTP Request nodes or `Set` nodes instead of stored as credentials).
- [ ] No node parameter contains a real phone number, name, email, or webhook URL.
- [ ] The file opens and imports cleanly into a clean n8n instance with no errors.
- [ ] The companion `.md` file declares the tested n8n version.

If you're unsure whether something counts as sensitive, leave it out and describe it generically instead (e.g. "your CRM's contact-lookup endpoint").

## PR Checklist

- [ ] The linked project (if any) has commits within the last 6 months, or is a stable official reference.
- [ ] I personally followed the setup steps in the linked README, or personally ran the workflow I'm submitting.
- [ ] License is clear (MIT/Apache/CC0/etc.) for any linked project.
- [ ] Description is under ~15 words and free of marketing language.
- [ ] Entry is added to the correct section, in a sensible order.
- [ ] (Workflow submissions only) I completed the sanitization checklist above and declared the tested n8n version.

## Reporting a mistake

If you spot a broken link, an unverified claim, or — most importantly — an accidentally leaked secret in this repository, see [SECURITY.md](SECURITY.md).
