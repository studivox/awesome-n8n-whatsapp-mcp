#!/usr/bin/env python3
"""Repository validator for awesome-n8n-whatsapp-mcp.

Standard-library only. Run with: python3 scripts/validate_repository.py

This is a defensive quality gate, not a guarantee. It cannot detect every
possible secret, credential, or unsafe pattern -- it catches common, obvious
mistakes before they reach a public branch. Human review of every workflow
before import and activation is still required (see SECURITY.md).
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / "workflows"

# ---------------------------------------------------------------------------
# Shared placeholder allow-list -- values that look secret-shaped but are
# documented, synthetic dummy data and must not be rejected.
# ---------------------------------------------------------------------------
PLACEHOLDER_TOKENS = (
    "YOUR_",
    "REPLACE_ME",
    "CHANGE_ME",
    "CHANGEME",
    "EXAMPLE",
    "PLACEHOLDER",
    "XXXXXXXX",
    "TEST_",
    "DUMMY",
    "SAMPLE",
    "<YOUR",
    "{{",  # templated placeholder syntax
    "${",
)
ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "yourdomain.com",
    "yourcompany.com",
}
ALLOWED_HOST_FRAGMENTS = (
    "your_n8n_instance",
    "your-n8n-instance",
    "example.com",
    "localhost",
    "127.0.0.1",
    "yourdomain",
)

REQUIRED_MD_HEADINGS = [
    "what it does",
    "real business use case",
    "required n8n version",
    "required nodes",
    "required credentials",
    "environment variables",
    "setup steps",
    "test procedure",
    "known limitations",
    "data handled",
    "license and source",
    "last verification date",
]

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    (
        "private key block",
        re.compile(r"BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY"),
    ),
    (
        "inline secret assignment",
        re.compile(
            r"(api[_-]?key|secret|access[_-]?token|bearer|password|auth[_-]?token)"
            r'\s*[:=]\s*["\']?([A-Za-z0-9_\-\.]{16,})["\']?',
            re.IGNORECASE,
        ),
    ),
]

# Excludes matches embedded inside hyphenated hex identifiers (e.g. n8n node/
# webhook UUIDs like "039b402e-4666-4405-86c3-..."), which otherwise false-
# positive as phone numbers since UUIDs are digit-and-hyphen-heavy.
PHONE_PATTERN = re.compile(r"(?<![0-9a-fA-F-])\+?\d[\d\-\s]{8,16}\d(?![0-9a-fA-F-])")
WEBHOOK_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]*webhook[^\s\"'<>]*", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
MD_LINK_PATTERN = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SVG_UNSAFE_ATTR = re.compile(
    r"(?:xlink:href|href|src)\s*=\s*\"(https?:)?//[^\"]*\"", re.IGNORECASE
)
SVG_EVENT_HANDLER = re.compile(r"\son[a-zA-Z]+\s*=", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Fail-closed commitment placeholder policy (see
# workflows/whatsapp-webhook-security-gateway.md "Security design"). A
# workflow's Code node may assign an EXPECTED_COMMITMENT constant that is
# meant to be replaced by each user's own setup value before use. The
# distributable copy in this repository must ship with ONLY the one
# approved, unmistakable placeholder -- never a real-looking 64-character
# hex commitment (which could be mistaken for configured production
# material) and never anything else that looks like it was pasted in by
# accident during testing.
# ---------------------------------------------------------------------------
APPROVED_COMMITMENT_PLACEHOLDER = "REPLACE_WITH_YOUR_COMMITMENT__SEE_SETUP_STEP_3"
COMMITMENT_ASSIGNMENT_PATTERN = re.compile(
    r"EXPECTED_COMMITMENT\s*=\s*['\"]([^'\"]*)['\"]"
)
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class Report:
    def __init__(self):
        self.errors = []

    def error(self, path, message):
        self.errors.append(f"{path}: {message}")

    def ok(self):
        return not self.errors


def is_placeholder(value):
    upper = value.upper()
    return any(token in upper for token in PLACEHOLDER_TOKENS)


def looks_like_real_phone(match_text):
    digits = re.sub(r"\D", "", match_text)
    if len(digits) < 9 or len(digits) > 15:
        return False
    if len(set(digits)) <= 2:
        # e.g. 00000000000, 11111111111, or alternating placeholder digits
        return False
    return True


def looks_like_real_webhook(url):
    lowered = url.lower()
    return not any(fragment in lowered for fragment in ALLOWED_HOST_FRAGMENTS)


def looks_like_real_email(domain):
    return domain.lower() not in ALLOWED_EMAIL_DOMAINS


def scan_text_for_secrets(path, text, report):
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if is_placeholder(value):
                continue
            report.error(path, f"possible {label} found ({value[:40]!r}...)")

    for match in PHONE_PATTERN.finditer(text):
        candidate = match.group(0)
        if is_placeholder(candidate):
            continue
        if looks_like_real_phone(candidate):
            report.error(
                path,
                f"possible real phone number {candidate!r} -- use a documented placeholder",
            )

    for match in WEBHOOK_URL_PATTERN.finditer(text):
        url = match.group(0)
        if is_placeholder(url):
            continue
        if looks_like_real_webhook(url):
            report.error(path, f"possible private webhook URL {url!r}")

    for match in EMAIL_PATTERN.finditer(text):
        domain = match.group(1)
        full = match.group(0)
        if is_placeholder(full):
            continue
        if looks_like_real_email(domain):
            report.error(path, f"possible real email address {full!r}")


# ---------------------------------------------------------------------------
# Workflow package validation
# ---------------------------------------------------------------------------

def validate_commitment_placeholders(path, data, report):
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return

    for node in nodes:
        if not isinstance(node, dict):
            continue
        params = node.get("parameters")
        js_code = params.get("jsCode") if isinstance(params, dict) else None
        if not isinstance(js_code, str):
            continue

        for match in COMMITMENT_ASSIGNMENT_PATTERN.finditer(js_code):
            value = match.group(1)
            if value == APPROVED_COMMITMENT_PLACEHOLDER:
                continue
            node_name = node.get("name", "?")
            if HEX64_PATTERN.match(value):
                report.error(
                    path,
                    f"node {node_name!r} EXPECTED_COMMITMENT looks like a real "
                    "64-character hex commitment, not the approved placeholder "
                    f"({APPROVED_COMMITMENT_PLACEHOLDER!r}) -- do not commit real or "
                    "generated setup material; the distributable workflow must ship "
                    "with only the approved placeholder",
                )
            else:
                report.error(
                    path,
                    f"node {node_name!r} EXPECTED_COMMITMENT is neither the approved "
                    f"placeholder ({APPROVED_COMMITMENT_PLACEHOLDER!r}) nor empty -- "
                    "unexpected value, verify it is not accidentally-committed setup "
                    "material",
                )


def validate_workflow_json(path, report):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.error(path, f"could not read file: {exc}")
        return

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        report.error(path, f"invalid JSON: {exc}")
        return

    if not isinstance(data, dict):
        report.error(path, "JSON root must be an object (n8n workflow export)")
        return

    nodes = data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0:
        report.error(path, "workflow JSON must contain a non-empty 'nodes' array")

    connections = data.get("connections")
    if not isinstance(connections, dict):
        report.error(path, "workflow JSON must contain a 'connections' object")

    validate_commitment_placeholders(path, data, report)
    scan_text_for_secrets(path, text, report)


def validate_workflow_md(path, report):
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()

    for heading in REQUIRED_MD_HEADINGS:
        pattern = re.compile(
            r"^#{1,3}\s*" + re.escape(heading), re.IGNORECASE | re.MULTILINE
        )
        if not pattern.search(lowered):
            report.error(path, f"missing required heading: '{heading}'")

    scan_text_for_secrets(path, text, report)


def validate_workflow_pairs(report):
    if not WORKFLOWS_DIR.is_dir():
        report.error(WORKFLOWS_DIR, "workflows/ directory is missing")
        return 0

    json_files = {
        p.stem: p for p in WORKFLOWS_DIR.glob("*.json")
    }
    md_files = {
        p.stem: p
        for p in WORKFLOWS_DIR.glob("*.md")
        if p.name.lower() != "readme.md"
    }

    for stem, json_path in json_files.items():
        if stem not in md_files:
            report.error(json_path, f"no matching {stem}.md documentation file found")

    for stem, md_path in md_files.items():
        if stem not in json_files:
            report.error(md_path, f"no matching {stem}.json workflow export found")

    validated = 0
    for stem in sorted(set(json_files) & set(md_files)):
        validate_workflow_json(json_files[stem], report)
        validate_workflow_md(md_files[stem], report)
        validated += 1

    # Orphan files still get their own content checked so errors are specific.
    for stem, json_path in json_files.items():
        if stem not in md_files:
            validate_workflow_json(json_path, report)
    for stem, md_path in md_files.items():
        if stem not in json_files:
            validate_workflow_md(md_path, report)

    return validated


# ---------------------------------------------------------------------------
# Markdown internal link validation
# ---------------------------------------------------------------------------

def validate_markdown_links(report):
    for md_path in sorted(ROOT.rglob("*.md")):
        if ".git" in md_path.parts:
            continue
        text = md_path.read_text(encoding="utf-8")
        for match in MD_LINK_PATTERN.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            clean_target = target.split("#", 1)[0]
            if not clean_target:
                continue
            resolved = (md_path.parent / clean_target).resolve()
            if not resolved.exists():
                report.error(md_path, f"broken relative link: '{target}'")


# ---------------------------------------------------------------------------
# SVG validation
# ---------------------------------------------------------------------------

def validate_svg_files(report):
    svg_files = [p for p in ROOT.rglob("*.svg") if ".git" not in p.parts]
    for svg_path in sorted(svg_files):
        text = svg_path.read_text(encoding="utf-8")

        try:
            ET.fromstring(text)
        except ET.ParseError as exc:
            report.error(svg_path, f"not well-formed XML: {exc}")
            continue

        if "<script" in text.lower():
            report.error(svg_path, "contains a forbidden <script> element")
        if "foreignobject" in text.lower():
            report.error(svg_path, "contains a forbidden <foreignObject> element")
        if SVG_EVENT_HANDLER.search(text):
            report.error(svg_path, "contains a forbidden inline event-handler attribute")
        if SVG_UNSAFE_ATTR.search(text):
            report.error(svg_path, "references a remote resource (href/src to http(s))")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    report = Report()

    validated_count = validate_workflow_pairs(report)
    validate_markdown_links(report)
    validate_svg_files(report)

    print("Repository validation")
    print("----------------------")
    print(f"Workflow packages validated: {validated_count}")
    print(f"Markdown files link-checked: {len(list(p for p in ROOT.rglob('*.md') if '.git' not in p.parts))}")
    print(f"SVG files checked: {len(list(p for p in ROOT.rglob('*.svg') if '.git' not in p.parts))}")
    print()

    if report.ok():
        print("PASS -- no issues found.")
        print(
            "Note: this is a defensive quality gate, not a guarantee. It cannot "
            "detect every possible secret or unsafe pattern -- human review is "
            "still required for every workflow before import and activation."
        )
        return 0

    print(f"FAIL -- {len(report.errors)} issue(s) found:\n")
    for line in report.errors:
        print(f"  - {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
