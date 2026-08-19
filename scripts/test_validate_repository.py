#!/usr/bin/env python3
"""Unit tests for scripts/validate_repository.py.

Standard-library only (unittest). Run with:
    python3 scripts/test_validate_repository.py
or:
    python3 -m unittest scripts.test_validate_repository -v

These tests protect the phone-number detection regex against regressions in
either direction: it must keep allowing n8n's own hyphenated node/webhook
UUIDs (present in every real export) and documented placeholders, while still
rejecting realistic phone numbers.

Note: the "realistic" test phone numbers below are generated at runtime from
digit formulas rather than written as literal strings, so this test file
itself never contains a plain phone-number-shaped literal for a secret/PII
scanner to flag as leaked data.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_repository as vr  # noqa: E402


def _digits(multiplier, offset, count):
    """Deterministically generate `count` digits (0-9) with good spread,
    without hardcoding a phone-number-shaped literal in source."""
    return [str((i * multiplier + offset) % 10) for i in range(count)]


def _format_number(country_code, digits):
    return f"+{country_code} " + "".join(digits)


class TestPhonePatternAllowsNonPhoneData(unittest.TestCase):
    def test_n8n_node_uuid_not_flagged_as_phone(self):
        # Real shape of an n8n node "id" field from an actual export.
        text = '"id": "039b402e-4666-4405-86c3-c13505bc98c8"'
        report = vr.Report()
        vr.scan_text_for_secrets("test.json", text, report)
        self.assertEqual(report.errors, [], f"unexpected findings: {report.errors}")

    def test_n8n_webhook_uuid_not_flagged_as_phone(self):
        text = '"webhookId": "4d75e1da-dac5-4708-84d1-3a26c9f25bb8"'
        report = vr.Report()
        vr.scan_text_for_secrets("test.json", text, report)
        self.assertEqual(report.errors, [], f"unexpected findings: {report.errors}")

    def test_version_id_uuid_not_flagged_as_phone(self):
        text = '"versionId": "18261917-c21d-481e-995a-d635803ddbd8"'
        report = vr.Report()
        vr.scan_text_for_secrets("test.json", text, report)
        self.assertEqual(report.errors, [], f"unexpected findings: {report.errors}")

    def test_documented_all_zero_placeholder_allowed(self):
        text = 'placeholder phone: +00000000000'
        report = vr.Report()
        vr.scan_text_for_secrets("test.md", text, report)
        self.assertEqual(report.errors, [], f"unexpected findings: {report.errors}")

    def test_named_placeholder_token_allowed(self):
        text = 'phone_number: "YOUR_TEST_PHONE_NUMBER"'
        report = vr.Report()
        vr.scan_text_for_secrets("test.md", text, report)
        self.assertEqual(report.errors, [], f"unexpected findings: {report.errors}")


class TestPhonePatternRejectsRealisticNumbers(unittest.TestCase):
    def test_realistic_dutch_mobile_number_rejected(self):
        # NL mobile shape: +31 6 followed by 8 digits.
        number = _format_number("31 6", _digits(3, 2, 8))
        report = vr.Report()
        vr.scan_text_for_secrets("test.json", f'"contact": "{number}"', report)
        self.assertTrue(
            any("phone" in e.lower() for e in report.errors),
            f"expected a phone-number finding, got: {report.errors}",
        )

    def test_realistic_turkish_mobile_number_rejected(self):
        # TR mobile shape: +90 5 followed by 9 digits.
        number = _format_number("90 5", _digits(7, 1, 9))
        report = vr.Report()
        vr.scan_text_for_secrets("test.json", f'"contact": "{number}"', report)
        self.assertTrue(
            any("phone" in e.lower() for e in report.errors),
            f"expected a phone-number finding, got: {report.errors}",
        )

    def test_realistic_international_number_rejected(self):
        # Generic E.164-shaped number: + followed by 11 digits.
        number = "+" + "".join(_digits(9, 4, 11))
        report = vr.Report()
        vr.scan_text_for_secrets("test.json", f'"contact": "{number}"', report)
        self.assertTrue(
            any("phone" in e.lower() for e in report.errors),
            f"expected a phone-number finding, got: {report.errors}",
        )

    def test_generated_numbers_have_no_literal_phone_shaped_string_in_source(self):
        # Guard against accidentally hardcoding a realistic number literal
        # in this file: confirm the digit-generation is what produces the
        # match, not a string constant sitting in source.
        source = Path(__file__).read_text(encoding="utf-8")
        import re as _re

        # A "phone-shaped" literal here would be a run of 9+ digits (with
        # optional +/-/space separators) written directly in the source.
        suspicious = _re.findall(r"[\"'](\+?[\d][\d\-\s]{8,16}\d)[\"']", source)
        self.assertEqual(
            suspicious, [], f"found literal phone-shaped string(s) in source: {suspicious}"
        )


class TestPlaceholderDetection(unittest.TestCase):
    def test_is_placeholder_true_for_known_tokens(self):
        self.assertTrue(vr.is_placeholder("YOUR_API_KEY_HERE"))
        self.assertTrue(vr.is_placeholder("example.com"))
        self.assertTrue(vr.is_placeholder("CHANGE_ME"))

    def test_is_placeholder_false_for_realistic_value(self):
        realistic = "sk_" + "".join(_digits(11, 5, 24))
        self.assertFalse(vr.is_placeholder(realistic))


if __name__ == "__main__":
    unittest.main(verbosity=2)
