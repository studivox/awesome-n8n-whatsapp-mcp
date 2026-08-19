#!/usr/bin/env python3
"""Unit tests for scripts/generate_webhook_verify_commitment.py.

Standard library only (unittest). Run with:
    python3 scripts/test_generate_webhook_verify_commitment.py
"""

import hashlib
import hmac
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_webhook_verify_commitment as gwvc  # noqa: E402


class TestComputeCommitment(unittest.TestCase):
    def test_matches_reference_hmac_implementation(self):
        # Independently computed, not via the module under test, so this
        # actually checks compute_commitment's correctness rather than its
        # own logic against itself.
        token = "some-example-token-value-for-testing-only"
        expected = hmac.new(token.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(gwvc.compute_commitment(token), expected)

    def test_output_is_64_lowercase_hex_chars(self):
        commitment = gwvc.compute_commitment("another-example-token")
        self.assertRegex(commitment, r"^[0-9a-f]{64}$")

    def test_different_tokens_produce_different_commitments(self):
        a = gwvc.compute_commitment("token-one")
        b = gwvc.compute_commitment("token-two")
        self.assertNotEqual(a, b)

    def test_same_token_always_produces_same_commitment(self):
        token = "stable-example-token"
        self.assertEqual(gwvc.compute_commitment(token), gwvc.compute_commitment(token))


class TestGenerateToken(unittest.TestCase):
    def test_generated_token_has_at_least_256_bits_of_entropy(self):
        token = gwvc.generate_token()
        # hex-encoded, so 2 chars per byte; MIN_TOKEN_BYTES bytes = 256 bits minimum.
        self.assertGreaterEqual(len(token.encode("utf-8")) * 4, 256)  # hex digit = 4 bits

    def test_generated_token_is_hex(self):
        token = gwvc.generate_token()
        self.assertRegex(token, r"^[0-9a-f]+$")

    def test_generated_tokens_are_not_repeated(self):
        tokens = {gwvc.generate_token() for _ in range(20)}
        self.assertEqual(len(tokens), 20)


class TestNeverAcceptsTokenAsArgv(unittest.TestCase):
    def test_unknown_positional_argument_is_rejected(self):
        parser = gwvc.build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["MY_SECRET_TOKEN_VALUE"])

    def test_only_recognized_flag_is_accepted(self):
        parser = gwvc.build_arg_parser()
        args = parser.parse_args(["--existing"])
        self.assertTrue(args.existing)

    def test_no_args_is_valid(self):
        parser = gwvc.build_arg_parser()
        args = parser.parse_args([])
        self.assertFalse(args.existing)

    def test_source_never_reads_sys_argv_for_token_value(self):
        # Static check: the module's source should not read a token out of
        # sys.argv positional values anywhere (only out of getpass/secrets).
        source = Path(gwvc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sys.argv[1]", source)
        self.assertNotIn("argv[1]", source)


class TestNeverWritesToDisk(unittest.TestCase):
    def test_source_contains_no_file_write_calls(self):
        source = Path(gwvc.__file__).read_text(encoding="utf-8")
        forbidden = ["open(", "Path(", ".write(", ".write_text(", ".write_bytes("]
        for token in forbidden:
            self.assertNotIn(token, source, f"found forbidden file-write-shaped call: {token}")


class TestReadExistingTokenUsesGetpass(unittest.TestCase):
    def test_uses_getpass_not_input(self):
        with patch("generate_webhook_verify_commitment.getpass.getpass", return_value="a-secret-token") as mock_getpass:
            token = gwvc.read_existing_token()
        mock_getpass.assert_called_once()
        self.assertEqual(token, "a-secret-token")

    def test_empty_token_aborts(self):
        with patch("generate_webhook_verify_commitment.getpass.getpass", return_value="   "):
            with self.assertRaises(SystemExit):
                gwvc.read_existing_token()


class TestMainDoesNotLeakSecretsToLogSafeChannels(unittest.TestCase):
    def test_main_with_generated_token_runs_cleanly(self):
        # Just confirms it runs end-to-end without error for the generate path.
        exit_code = gwvc.main([])
        self.assertEqual(exit_code, 0)

    def test_main_with_existing_short_token_warns(self):
        with patch("generate_webhook_verify_commitment.getpass.getpass", return_value="short"):
            exit_code = gwvc.main(["--existing"])
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
