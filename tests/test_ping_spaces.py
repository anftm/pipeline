"""Offline contracts for six-hour keepalive and BHA's actual readiness gate."""

from contextlib import ExitStack
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts import ping_spaces as ping


READY = {"ok": True, "index_ready": True, "index_status": "ready", "es": True}
BHA = ping.SPACES[-1]


class PingSpacesTests(unittest.TestCase):
    def setUp(self):
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.urlopen = contexts.enter_context(patch.object(ping, "urlopen"))
        contexts.enter_context(patch("builtins.print"))

    def response(self, body=b"", status=200):
        response = Mock(status=status)
        response.read.return_value = body
        self.urlopen.return_value.__enter__.return_value = response
        return response

    def test_bha_requires_every_ready_field_with_real_booleans(self):
        self.assertTrue(ping.bha_ready(READY))
        for field in READY:
            missing = dict(READY)
            missing.pop(field)
            self.assertFalse(ping.bha_ready(missing), field)
        for field in ("ok", "index_ready", "es"):
            for value in (False, 1, "true", None):
                self.assertFalse(ping.bha_ready({**READY, field: value}), (field, value))
        self.assertFalse(ping.bha_ready({**READY, "index_status": "restoring"}))
        self.assertFalse(ping.bha_ready([]))

    def test_bha_ready_json_passes_without_authentication(self):
        response = self.response(json.dumps(READY).encode())
        healthy, retryable, _ = ping.probe(BHA, 12)
        self.assertTrue(healthy)
        self.assertFalse(retryable)
        response.read.assert_called_once_with(ping.MAX_HEALTH_BYTES + 1)
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, BHA.url)
        self.assertEqual(request.get_method(), "GET")
        self.assertFalse(request.has_header("Authorization"))
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 12)

    def test_http_200_homepage_or_incomplete_health_is_not_ready(self):
        for body in (b"<html>Space is waking up</html>", b"null", b"{}",
                     b'{"ok":true}', json.dumps({**READY, "es": False}).encode()):
            with self.subTest(body=body):
                self.response(body)
                healthy, retryable, _ = ping.probe(BHA, 5)
                self.assertFalse(healthy)
                self.assertTrue(retryable)

    def test_health_body_is_bounded(self):
        self.response(b"x" * (ping.MAX_HEALTH_BYTES + 1))
        self.assertEqual(ping.probe(BHA, 5)[:2], (False, True))

    def test_other_spaces_preserve_http_200_homepage_contract(self):
        response = self.response(b"not downloaded")
        self.assertEqual(ping.probe(ping.SPACES[0], 5)[:2], (True, False))
        response.read.assert_not_called()

    def test_transient_http_and_network_errors_can_retry(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.urlopen.side_effect = HTTPError(BHA.url, status, "temporary", {}, None)
                self.assertEqual(ping.probe(BHA, 5)[:2], (False, True))
        for error in (URLError("connection reset"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                self.assertEqual(ping.probe(BHA, 5)[:2], (False, True))

    def test_deterministic_http_errors_do_not_retry(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.urlopen.side_effect = HTTPError(BHA.url, status, "permanent", {}, None)
                self.assertEqual(ping.probe(BHA, 5)[:2], (False, False))

    def test_retry_waits_for_actual_readiness(self):
        with patch.object(ping.time, "monotonic", return_value=0), \
                patch.object(ping.time, "sleep") as sleep, \
                patch.object(ping, "probe", side_effect=[
                    (False, True, "restoring"), (True, False, "ready")]) as probe:
            self.assertTrue(ping.check_space(BHA))
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(ping.RETRY_DELAY_SECONDS)

    def test_total_wait_and_final_request_use_remaining_budget(self):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        space = ping.SpaceCheck("short check", BHA.url, True, 20)
        with patch.object(ping.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(ping.time, "sleep", side_effect=sleep), \
                patch.object(ping, "probe", return_value=(False, True, "restoring")) as probe:
            self.assertFalse(ping.check_space(space))
        self.assertEqual(now[0], 20)
        self.assertEqual([call.args[1] for call in probe.call_args_list], [20, 5])

    def test_permanent_failure_does_not_sleep(self):
        with patch.object(ping.time, "sleep") as sleep, \
                patch.object(ping, "probe", return_value=(False, False, "HTTP 404")):
            self.assertFalse(ping.check_space(BHA))
        sleep.assert_not_called()

    def test_one_failed_space_does_not_skip_any_other_space(self):
        with patch.object(ping, "check_space", side_effect=lambda space: space != BHA) as check:
            self.assertEqual(ping.main(), 1)
        self.assertCountEqual([call.args[0] for call in check.call_args_list], ping.SPACES)
        with patch.object(ping, "check_space", return_value=True):
            self.assertEqual(ping.main(), 0)

    def test_workflow_is_six_hour_off_peak_read_only_and_bounded(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/ops-keep-spaces-alive.yml").read_text()
        self.assertIn("cron: '17 2,8,14,20 * * *'", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("timeout-minutes: 10", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("python -B scripts/ping_spaces.py", workflow)
        self.assertIn("unittest tests.test_ping_spaces", workflow)
        self.assertNotIn("reader-sidecar", workflow)
        self.assertNotIn("HF_TOKEN", workflow)
        self.assertEqual(len(ping.SPACES), 4)
        self.assertEqual(BHA.url, "https://vomebook-bha-search.hf.space/api/health")


if __name__ == "__main__":
    unittest.main()
