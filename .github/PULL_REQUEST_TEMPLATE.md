## What does this PR add/change?

<!-- One or two sentences. -->

## Type of contribution

- [ ] New resource/project link
- [ ] New workflow (`.json` + `.md` pair in `workflows/`)
- [ ] Fix (broken link, typo, correction)
- [ ] Other (describe above)

## If adding a resource link

- [ ] I opened the link myself and confirmed it works.
- [ ] The linked project has commits within the last 6 months, or is a stable official reference.
- [ ] License is clear for commercial use.
- [ ] I tested it here: <!-- describe what you did and what version, if applicable -->

## If adding a workflow

- [ ] Both `workflow-slug.json` and `workflow-slug.md` are included.
- [ ] The `.md` file includes every required heading (see [`workflows/README.md`](../workflows/README.md#submission-format)) and declares the exact tested n8n version.
- [ ] I completed the full [sanitization checklist](../CONTRIBUTING.md#mandatory-sanitization--no-exceptions) — no API keys, tokens, phone numbers, customer data, or real webhook URLs remain anywhere in the JSON.
- [ ] I re-imported the sanitized JSON into a clean n8n instance to confirm it still works.
- [ ] `python3 scripts/validate_repository.py` passes locally.

## Anything else reviewers should know?

<!-- Optional -->
