from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ui import quota_usage


class QuotaUsageParserTests(unittest.TestCase):
    def test_parses_five_hour_and_weekly_as_used_percent(self):
        payload = {
            "command": {"data": {"groups": [
                {"displayName": "Gemini Models", "buckets": [
                    {"window": "5h", "remaining_fraction": 0.72,
                     "reset_time": "2026-10-01T12:00:00Z"},
                    {"window": "weekly", "remaining_fraction": 0.41,
                     "reset_time": "2026-10-05T12:00:00Z"},
                ]},
                {"displayName": "Claude and GPT models", "buckets": [
                    {"bucketId": "3p-5h", "remainingFraction": 0.9},
                    {"bucketId": "3p-weekly", "remainingFraction": 0.25},
                ]},
            ]}}
        }
        groups = quota_usage.parse_usage_output(json.dumps(payload))
        self.assertEqual(groups[0]["id"], "gemini")
        self.assertEqual(groups[0]["windows"]["five_hour"]["used_percent"], 28.0)
        self.assertEqual(groups[0]["windows"]["weekly"]["used_percent"], 59.0)
        self.assertEqual(groups[1]["id"], "claude_gpt")
        self.assertEqual(groups[1]["windows"]["five_hour"]["used_percent"], 10.0)
        self.assertEqual(groups[1]["windows"]["weekly"]["used_percent"], 75.0)

    def test_rejects_log_prefix_and_accepts_exact_json_only(self):
        payload = {"data": {"groups": [{
            "display_name": "Gemini Models",
            "buckets": [
                {"window": "5_hour", "remainingFraction": 0.8},
                {"window": "7d", "remainingFraction": 0.2},
            ],
        }]}}
        self.assertEqual(
            quota_usage.parse_usage_output("agy info line\n" + json.dumps(payload)), []
        )
        groups = quota_usage.parse_usage_output(json.dumps(payload))
        self.assertEqual(groups[0]["windows"]["five_hour"]["used_percent"], 20.0)
        self.assertEqual(groups[0]["windows"]["weekly"]["used_percent"], 80.0)

    def test_duplicate_window_uses_most_constrained_bucket(self):
        payload = {"groups": [{"name": "Gemini", "buckets": [
            {"window": "5h", "remaining_fraction": 0.8, "reset_time": "later"},
            {"window": "5h", "remaining_fraction": 0.3, "reset_time": "sooner"},
        ]}]}
        groups = quota_usage.parse_usage_output(json.dumps(payload))
        self.assertEqual(groups[0]["windows"]["five_hour"]["used_percent"], 70.0)
        self.assertEqual(groups[0]["windows"]["five_hour"]["reset_time"], "sooner")

    def test_parses_exact_nested_response_json(self):
        inner = {"groups": [{"name": "Gemini", "buckets": [
            {"window": "weekly", "remaining_fraction": 0.6}
        ]}]}
        outer = {"status": "SUCCESS", "response": json.dumps(inner)}
        groups = quota_usage.parse_usage_output(json.dumps(outer))
        self.assertEqual(groups[0]["windows"]["weekly"]["used_percent"], 40.0)

    def test_rejects_unknown_windows_and_non_finite_or_non_fraction_values(self):
        for window, value in (("24_hour", 0.3), ("15h", 0.3), ("1 hour", 0.3),
                              ("5h", float("nan")), ("5h", float("inf")),
                              ("5h", True), ("5h", 50), ("5h", -0.1)):
            payload = {"groups": [{"name": "Gemini", "buckets": [
                {"window": window, "remaining_fraction": value},
            ]}]}
            self.assertEqual(quota_usage.parse_usage_output(json.dumps(payload)), [])

    def test_rejects_conflicting_or_unknown_explicit_windows(self):
        cases = [
            {"window": "24_hour", "bucket_id": "gemini-5h", "remaining_fraction": 0.5},
            {"window": "5h", "bucket_id": "gemini-weekly", "remaining_fraction": 0.5},
        ]
        for bucket in cases:
            payload = {"groups": [{"name": "Gemini", "buckets": [bucket]}]}
            self.assertEqual(quota_usage.parse_usage_output(json.dumps(payload)), [])

    def test_returns_empty_when_no_quota_groups_exist(self):
        self.assertEqual(quota_usage.parse_usage_output('{"status":"ok"}'), [])

    def test_parses_observed_agy_1_2_14_usage_fixture(self):
        text = (ROOT / "tests" / "fixtures" / "agy_usage_1_2_14.json").read_text()
        groups = quota_usage.parse_usage_output(text)
        self.assertEqual([g["id"] for g in groups], ["gemini", "claude_gpt"])
        self.assertEqual(groups[0]["windows"]["five_hour"]["used_percent"], 9.0)
        self.assertEqual(groups[0]["windows"]["weekly"]["used_percent"], 29.0)
        quota_usage.validate_usage_envelope(text)

    def test_nonzero_token_usage_is_rejected(self):
        payload = {"status": "SUCCESS", "response": "Gemini Models\tWeekly Limit Remaining\t50%\t2026-10-03T00:00:00Z",
                   "usage": {"input_tokens": 1, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 1}}
        with self.assertRaisesRegex(RuntimeError, "quota_command_consumed_tokens"):
            quota_usage.validate_usage_envelope(json.dumps(payload))


class QuotaUsageProbeTests(unittest.TestCase):
    def test_probe_uses_isolated_volume_and_usage_slash_command(self):
        payload = {"groups": [{"display_name": "Gemini Models", "buckets": [
            {"window": "5h", "remaining_fraction": 0.5},
            {"window": "weekly", "remaining_fraction": 0.8},
        ]}]}
        fake = __import__('subprocess').CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        from unittest import mock
        with mock.patch.object(quota_usage.subprocess, "run", return_value=fake) as run:
            groups = quota_usage.probe_account_usage(account="A1", account_volume="acct-volume",
                                                      image="agy:test", timeout=20)
        cmd = run.call_args.args[0]
        self.assertIn("acct-volume:/home/agy", cmd)
        self.assertIn("/usage", cmd)
        self.assertIn("agy.multiplex.account=A1", cmd)
        self.assertIn("agy.multiplex.kind=quota", cmd)
        self.assertIn("--name", cmd)
        self.assertNotIn(str(Path.home()), " ".join(cmd))
        self.assertEqual(groups[0]["windows"]["five_hour"]["used_percent"], 50.0)

    def test_probe_fails_closed_when_usage_payload_missing(self):
        fake = __import__('subprocess').CompletedProcess([], 0, stdout='{"status":"SUCCESS","response":"","usage":{"total_tokens":0}}', stderr="")
        from unittest import mock
        with mock.patch.object(quota_usage.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "quota_payload_unavailable"):
                quota_usage.probe_account_usage(account="A1", account_volume="acct-volume",
                                                  image="agy:test", timeout=20)

    def test_probe_never_parses_stderr_as_quota(self):
        from unittest import mock
        fixture = (ROOT / "tests" / "fixtures" / "agy_usage_1_2_14.json").read_text()
        fake = __import__('subprocess').CompletedProcess([], 0, stdout='not-json', stderr=fixture)
        with mock.patch.object(quota_usage.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "quota_payload_unavailable"):
                quota_usage.probe_account_usage(account="A1", account_volume="acct-volume",
                                                  image="agy:test", timeout=20)

    def test_probe_cleans_named_container_on_timeout(self):
        from unittest import mock
        timeout = __import__('subprocess').TimeoutExpired(cmd=["docker"], timeout=20)
        cleanup = __import__('subprocess').CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(quota_usage.subprocess, "run", side_effect=[timeout, cleanup]) as run:
            with self.assertRaises(__import__('subprocess').TimeoutExpired):
                quota_usage.probe_account_usage(account="A1", account_volume="acct-volume",
                                                  image="agy:test", timeout=20)
        cleanup_cmd = run.call_args_list[1].args[0]
        self.assertEqual(cleanup_cmd[:3], ["docker", "rm", "-f"])


if __name__ == "__main__":
    unittest.main()
