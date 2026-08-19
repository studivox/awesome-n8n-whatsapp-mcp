# Security Policy

This repository is a curated resource list plus a small, growing set of sanitized n8n workflow exports. It intentionally contains no application code, no credentials, and no live infrastructure. Even so, mistakes happen — most commonly an unsanitized workflow export accidentally including a real API key, access token, phone number, customer name, or webhook URL.

Automated checks (`scripts/validate_repository.py`, run on every pull request) catch common, obvious patterns, but this is a **defensive quality gate, not a guarantee** — it cannot detect every possible secret or unsafe pattern. Importing any workflow from this repository still requires reviewing every node yourself before activation.

## Reporting an exposed secret

If you find what looks like a real (not placeholder) secret anywhere in this repository — in a `.json` workflow export, a `.md` file, an issue, or a pull request — **do not open a public issue or PR comment quoting the secret.** A public report makes the exposure worse before it can be fixed.

Instead:

1. Use GitHub's private vulnerability reporting for this repository: open the **Security** tab on [the repository page](https://github.com/studivox/awesome-n8n-whatsapp-mcp) and select **"Report a vulnerability"**, if enabled.
2. If that isn't available, contact a maintainer directly through their GitHub profile with a private message, and reference only:
   - Which file and line contains the secret.
   - What kind of secret it appears to be (API key, phone number, webhook URL, etc.) — without pasting the secret value itself into any public channel.

## What happens after a report

Once a leaked secret is confirmed:

1. The offending commit(s) will be removed from the repository, and history will be rewritten if necessary to purge the value (force-push with notice to contributors).
2. The reporter (if the affected party) is strongly advised to treat the exposed credential as compromised and rotate/revoke it immediately at the source (WhatsApp Business Manager, database provider, n8n instance, etc.) — removing it from Git history does not undo exposure if it was ever pushed to a public remote.
3. The pull request or file that introduced it will be reviewed to understand how it bypassed both the automated validator and the [manual sanitization checklist](CONTRIBUTING.md#mandatory-sanitization--no-exceptions), and both will be updated if they need to be stricter.

## Scope

This policy covers accidental secret exposure in this repository's own content. It does not cover vulnerabilities in third-party projects linked from the README — please report those to the linked project directly.
