import contextlib
import io
import json
import shlex
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import quota


def card(provider="remote/codex", **extra):
    return {
        "provider": provider,
        "windows": [{"name": "5 hours", "remaining": 0, "used": 100}],
        **extra,
    }


class QuotaTests(unittest.TestCase):
    def run_cli(self, args, rows=None, error=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("quota.fetch_quotas", return_value=rows, side_effect=error),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = quota.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_suffix_and_exact_match_priority(self):
        rows = [card(), card("codex")]
        self.assertEqual(quota.select_quotas(rows, "CODEX"), [rows[1]])
        self.assertEqual(quota.select_quotas(rows[:1], "codex"), rows[:1])

    def test_ambiguous_suffix_requires_full_id(self):
        with self.assertRaisesRegex(quota.QuotaError, "ambiguous"):
            quota.select_quotas([card(), card("other/codex")], "codex")

    def test_multiple_accounts_are_preserved(self):
        rows = [card(user="a@example.com"), card(user="b@example.com")]
        self.assertEqual(len(quota.select_quotas(rows, "codex")), 2)
        self.assertNotIn("user", quota.compact(rows[0]))

    def test_no_substring_match(self):
        with self.assertRaisesRegex(quota.QuotaError, "not found"):
            quota.select_quotas([card()], "code")

    def test_zero_and_unlimited_are_distinct(self):
        result = quota.compact(card())
        self.assertEqual(result["windows"][0]["remaining_pct"], 0)
        result = quota.compact(card(windows=[{
            "name": "Limit", "remaining": 100, "unlimited": True,
        }]))
        self.assertEqual(result["windows"], [{"name": "Limit", "unlimited": True}])

    def test_balance_and_count_units_are_preserved(self):
        result = quota.compact(card(
            balance="CNY 0.00",
            windows=[{"name": "Month", "remaining": 25, "amount": 3, "limit": 4, "unit": "credits"}],
        ))
        self.assertEqual(result["balance"], "CNY 0.00")
        self.assertEqual(result["windows"][0]["amount"], 3)
        self.assertEqual(result["windows"][0]["unit"], "credits")

    def test_cached_error_does_not_discard_old_reading(self):
        row = card(error="upstream unavailable", readAt="2026-10-24T00:00:00Z",
                   asOf="2026-10-24T00:00:00Z", readDeferred=True)
        code, out, err = self.run_cli(["codex", "--json"], [row])
        result = json.loads(out)[0]
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertTrue(result["readDeferred"])
        self.assertEqual(result["readAt"], row["readAt"])
        self.assertEqual(result["asOf"], row["asOf"])
        self.assertIn("windows", result)

    def test_missing_quota_is_not_zero(self):
        result = quota.compact(card(windows=[]))
        self.assertEqual(result["error"], "quota unavailable")
        self.assertNotIn("balance", result)

    def test_list_is_compact_and_deduplicated(self):
        code, out, err = self.run_cli(["--list"], [card(), card(), card("remote/zai")])
        self.assertEqual(code, 0)
        self.assertEqual(out, "remote/codex\nremote/zai\n")
        self.assertEqual(err, "")

    def test_default_is_compact_text(self):
        code, out, err = self.run_cli(["codex"], [card()])
        self.assertEqual(code, 0)
        self.assertEqual(out, "codex 5h=0%\n")
        self.assertEqual(err, "")

    def test_json_remains_one_line_array(self):
        code, out, err = self.run_cli(["codex", "--json"], [card()])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIsInstance(json.loads(out), list)
        self.assertEqual(err, "")

    def test_failures_are_text_on_stderr(self):
        code, out, err = self.run_cli(["missing"], [card()])
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error="))
        code, out, err = self.run_cli([], error=quota.QuotaError("gateway HTTP 401"))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error='gateway HTTP 401'\n")

    def test_json_errors_and_list_remain_machine_readable(self):
        code, out, err = self.run_cli(["--json"], error=quota.QuotaError("gateway HTTP 401"))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err), {"error": "gateway HTTP 401"})
        code, out, err = self.run_cli(["--json", "--unknown"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("error", json.loads(err))
        code, out, err = self.run_cli(["--list", "--json"], [card()])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), ["remote/codex"])

    def test_bad_arguments_are_text(self):
        for args in (["--unknown"], ["--timeout", "nan"], ["--timeout", "-1"],
                     ["--timeout", "0"], ["--timeout", "abc"]):
            with self.subTest(args=args):
                code, out, err = self.run_cli(args)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("error="))

    def test_detail_adds_metadata_only_when_requested(self):
        row = card(readAt="2026-10-25T10:00:00Z", until="2026-11-01T00:00:00Z",
                   windows=[{"name": "5 hours", "remaining": 82.5,
                             "resetsAt": "2026-10-25T14:00:00Z",
                             "amount": 0, "limit": 100, "unit": "credits"}])
        result = quota.compact(row)
        now = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
        brief = quota.render_text([result], ["remote/codex"], now=now)
        detail = quota.render_text([result], ["remote/codex"], detail=True, now=now)
        self.assertEqual(brief, "codex 5h=82.5%")
        for field in ("provider=remote/codex", "read=2026-10-25T10:00:00Z",
                      "until=2026-11-01T00:00:00Z", "5h_reset=2026-10-25T14:00:00Z",
                      "5h_used=0", "5h_limit=100", "5h_unit=credits"):
            self.assertIn(field, detail.split())

    def test_old_or_failed_readings_are_flagged_in_brief_output(self):
        now = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
        rows = [
            card(readAt="2026-10-25T08:00:00Z"),
            card(asOf="2026-10-25T09:59:59Z"),
            card(readDeferred=True),
            card(error="upstream unavailable"),
        ]
        for row in rows:
            with self.subTest(row=row):
                out = quota.render_text([quota.compact(row)], ["remote/codex"], now=now)
                self.assertIn("stale=true", out)
                self.assertIn("age=", out)
        out = quota.render_text([quota.compact(rows[0])], ["remote/codex"], now=now)
        self.assertIn("age=7200s", out)
        code, out, err = self.run_cli(["codex"], [rows[-1]])
        self.assertEqual((code, err), (1, ""))
        self.assertIn("error='upstream unavailable'", out)

    def test_invalid_read_time_is_not_invented(self):
        row = quota.compact(card(readDeferred=True, readAt="invalid"))
        out = quota.render_text([row], ["remote/codex"])
        self.assertIn("stale=true age=unknown deferred=true", out)

    def test_provider_shortening_checks_all_providers(self):
        code, out, err = self.run_cli(["remote/codex"], [card(), card("other/codex")])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "remote/codex 5h=0%\n")

    def test_accounts_remain_distinguishable_without_emails(self):
        code, out, err = self.run_cli([], [card(user="a@example.com"), card(user="b@example.com")])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "codex account=1 5h=0%\ncodex account=2 5h=0%\n")
        code, out, err = self.run_cli([], [card(alias="work"), card(alias="personal")])
        self.assertIn("account=work", out)
        self.assertIn("account=personal", out)

    def test_balance_and_error_whitespace_cannot_create_extra_lines(self):
        row = card(windows=[], balance="CNY 0.00", error="line one\nline two")
        code, out, err = self.run_cli([], [row])
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(len(out.splitlines()), 1)
        fields = shlex.split(out)
        self.assertIn("balance=CNY 0.00", fields)
        self.assertIn("error=line one\\nline two", fields)

    def test_normalized_window_names_are_unique(self):
        windows = [
            {"name": name} for name in
            ("5 hours", "7 days", "MCP \u00b7 Month", "A-B", "A B", "a_b_1")
        ]
        labels = quota.window_labels(windows)
        self.assertEqual(labels[:3], ["5h", "7d", "mcp_month"])
        self.assertEqual(len(labels), len(set(labels)))

    def test_unknown_and_unlimited_are_distinct_in_text(self):
        row = card(windows=[{"name": "Limit", "unlimited": True},
                            {"name": "Month"}, {"name": "Week", "remaining": 0}])
        code, out, err = self.run_cli([], [row])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "codex limit=unlimited month=unknown week=0%\n")
        code, out, err = self.run_cli([], [card(windows=[{"name": "Month"}])])
        self.assertEqual((code, err), (1, ""))
        self.assertIn("month=unknown", out)
        self.assertIn("error='quota unavailable'", out)

    def test_empty_result_is_explicit(self):
        for args in ([], ["--list"]):
            code, out, err = self.run_cli(args, [])
            self.assertEqual((code, out, err), (0, "empty\n", ""))

    def test_gateway_keys_are_excluded_from_every_output_format(self):
        rows = [
            card("remote/codex", windows=[{"name": "Limit", "unlimited": True}]),
            card("~keys"),
            card("remote/~keys", error="internal key error"),
            card("remote/nested/~keys"),
        ]
        for args in ([], ["--detail"], ["--list"], ["--json"], ["--list", "--json"]):
            with self.subTest(args=args):
                code, out, err = self.run_cli(args, rows)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("codex", out)
                self.assertNotIn("~keys", out)
                self.assertNotIn("internal key error", out)
        code, out, err = self.run_cli([], rows)
        self.assertEqual(out, "codex limit=unlimited\n")

    def test_gateway_keys_are_not_available_for_explicit_queries(self):
        for provider in ("~keys", "remote/~keys"):
            with self.subTest(provider=provider):
                code, out, err = self.run_cli([provider], [card(), card("remote/~keys")])
                self.assertEqual((code, out), (1, ""))
                self.assertIn("available: remote/codex", err)
                self.assertNotIn("~keys", err)

    @patch("quota.urllib.request.build_opener")
    def test_gateway_request_uses_token_timeout_and_correct_path(self, build):
        build.return_value.open.return_value.__enter__.return_value = io.BytesIO(
            json.dumps({"data": [card()]}).encode()
        )
        with patch.dict("os.environ", {"MAGPIE_TOKEN": "test-secret"}):
            self.assertEqual(quota.fetch_quotas("http://localhost:3425/", 7), [card()])
        args, kwargs = build.return_value.open.call_args
        self.assertEqual(args[0].full_url, "http://localhost:3425/v1/magpie/quotas")
        self.assertEqual(args[0].get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(kwargs, {"timeout": 7})

    @patch("quota.urllib.request.build_opener")
    def test_malformed_response_is_rejected(self, build):
        for payload in (b"not json", b"[]", b'{"data":null}', b'{"data":[null]}',
                        b'{"data":[{"provider":"x","windows":null}]}'):
            with self.subTest(payload=payload):
                build.return_value.open.return_value.__enter__.return_value = io.BytesIO(payload)
                with self.assertRaises(quota.QuotaError):
                    quota.fetch_quotas("http://localhost:3425", 1)

    @patch("quota.urllib.request.build_opener")
    def test_network_failure_does_not_leak_credentials(self, build):
        build.return_value.open.side_effect = urllib.error.URLError("test-secret")
        with self.assertRaises(quota.QuotaError) as caught:
            quota.fetch_quotas("http://localhost:3425", 1)
        self.assertNotIn("test-secret", str(caught.exception))

    def test_url_must_be_an_origin(self):
        for url in ("file:///tmp/data", "http://user@localhost:3425",
                    "http://localhost:3425/v1", "http://localhost:3425?x=1"):
            with self.subTest(url=url), self.assertRaises(quota.QuotaError):
                quota.fetch_quotas(url, 1)


if __name__ == "__main__":
    unittest.main()
