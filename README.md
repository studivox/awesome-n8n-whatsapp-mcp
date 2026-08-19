# Awesome n8n + WhatsApp + MCP 🇹🇷🇳🇱

> A curated list of **real, working** [n8n](https://n8n.io) automations, WhatsApp Business Cloud API integrations, and [MCP](https://modelcontextprotocol.io) (Model Context Protocol) servers/tooling — with a specific focus on **business automation for the Turkish and Dutch (NL) markets**: appointment booking, customer support, and invoicing (BTW/KVK for NL, e-Fatura for TR).

[![Awesome](https://awesome.re/badge.svg)](https://awesome.re)
![Maintenance](https://img.shields.io/badge/maintained-yes-green)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)
![License](https://img.shields.io/badge/license-CC0--1.0-blue)

This is not a link dump. Every entry here is either an official, verifiable resource or a real workflow that has actually been exported, sanitized, and tested — not a "we should add this someday" placeholder. See [Inclusion criteria](#inclusion-criteria) below.

**Language note:** this README is English-first for global discoverability. The project's practical focus — invoicing rules, community links, market context — is Turkish (TR) and Dutch (NL) business automation, and some linked resources are in Turkish or Dutch accordingly.

---

## Table of Contents

- [Why this list exists](#why-this-list-exists)
- [Inclusion criteria](#inclusion-criteria)
- [n8n — Setup & Infrastructure](#n8n--setup--infrastructure)
- [n8n — WhatsApp Cloud API](#n8n--whatsapp-cloud-api)
- [n8n — Postgres / Database Integration](#n8n--postgres--database-integration)
- [n8n — Google Calendar / Appointment Scheduling](#n8n--google-calendar--appointment-scheduling)
- [Invoicing / VAT / Accounting (NL & TR)](#invoicing--vat--accounting-nl--tr)
- [MCP Servers](#mcp-servers)
- [Claude Code Skills](#claude-code-skills)
- [Workflows](#workflows)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

---

## Why this list exists

General-purpose "awesome n8n" lists already exist and are large. What's missing is a list that:

- Focuses specifically on the **n8n + WhatsApp Cloud API + Postgres** combination for real business use cases (not "hello world" demos).
- Covers the **invoicing/VAT automation needs specific to the Dutch (ZZP/BTW/KVK) and Turkish markets** — resources that are otherwise scattered across local forums.
- Requires every workflow submission to be **sanitized and genuinely tested**, not fabricated or copy-pasted from documentation examples.

## Inclusion criteria

An entry must meet **all** of the following before it's added:

1. **It works.** Setup steps in its README have actually been followed and verified by the person opening the PR.
2. **It's actively maintained** (a commit within the last 6 months), or it's a stable official reference (e.g. platform documentation) that doesn't need commit activity.
3. **It solves a real problem** — not a toy/demo example.
4. **Its license is clear** for commercial use.

Placeholder entries, dead links, and "someone should build this" ideas are not accepted — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## n8n — Setup & Infrastructure

| Project | Description |
|---|---|
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | The core n8n repository — self-hosted workflow automation platform. |
| [n8n-io/n8n-docker-caddy](https://github.com/n8n-io/n8n-docker-caddy) | Official Docker + Caddy setup for a self-hosted n8n instance with automatic HTTPS. |

## n8n — WhatsApp Cloud API

| Resource | Description |
|---|---|
| [n8n WhatsApp Trigger node docs](https://docs.n8n.io/integrations/builtin/trigger-nodes/n8n-nodes-base.whatsapptrigger/) | Official node documentation for receiving inbound WhatsApp messages via the Meta Cloud API webhook. |
| [Meta WhatsApp Cloud API docs](https://developers.facebook.com/docs/whatsapp/cloud-api) | Official Meta reference for the WhatsApp Business Cloud API. |
| [WhatsApp Cloud API webhook setup guide](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks) | Official guide for configuring and verifying webhooks — the step most beginners get stuck on. |

Real, sanitized WhatsApp automation workflows (appointment reminders, order-status notifications, support routing) belong in [`workflows/`](workflows/README.md) as matched `.json` + `.md` pairs — see that folder for current status and submission format.

## n8n — Postgres / Database Integration

| Resource | Description |
|---|---|
| [n8n Postgres node docs](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.postgres/) | Official documentation for CRUD operations and connection settings. |

## n8n — Google Calendar / Appointment Scheduling

| Resource | Description |
|---|---|
| [n8n Google Calendar node docs](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.googlecalendar/) | Official documentation for OAuth setup and event CRUD operations. |

## Invoicing / VAT / Accounting (NL & TR)

| Resource | Description |
|---|---|
| [Belastingdienst — BTW voor ondernemers](https://www.belastingdienst.nl/wps/wcm/connect/bldcontenten/belastingdienst/business/vat/vat) | Official Dutch tax authority reference for VAT (BTW) rules applicable to freelancers/ZZP and businesses. |
| [e-Fatura Portalı (GİB)](https://www.efatura.gov.tr/) | Official Turkish Revenue Administration portal for e-Invoice (e-Fatura) / e-Archive (e-Arşiv) systems. |

## MCP Servers

| Project | Description |
|---|---|
| [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | The official reference collection of MCP servers. |
| [modelcontextprotocol/typescript-sdk](https://github.com/modelcontextprotocol/typescript-sdk) | Official TypeScript SDK for building your own MCP server. |
| [MCP quickstart — build a server](https://modelcontextprotocol.io/quickstart/server) | Official step-by-step guide to writing an MCP server from scratch. |

## Claude Code Skills

| Project | Description |
|---|---|
| [ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills) | A large curated collection of Claude Code skills and plugins. |

## Workflows

See [`workflows/README.md`](workflows/README.md) for the sanitized n8n workflow JSON files in this repository, the submission format, and the mandatory sanitization checklist.

**No workflow is described as "production-tested" until a real, sanitized `.json` export backing that claim has been added and validated.** Currently the `workflows/` folder contains only the submission format and template — no workflow files have been added yet.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full process, PR checklist, and data-sanitization rules.

## Security

See [SECURITY.md](SECURITY.md) for how to report an accidentally exposed secret (API key, token, phone number, webhook URL, etc.) found anywhere in this repository.

## License

[CC0 1.0 Universal](LICENSE) — this list's curation (README, CONTRIBUTING, and related project files) is released into the public domain. Every third-party project linked here is subject to its own license — check the linked project's own LICENSE file before use.
