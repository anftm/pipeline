import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from scripts import github_read


class GitHubReadTests(unittest.TestCase):
    def response(self, value):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(value).encode()
        return response

    def http_error(self, status, body):
        return urllib.error.HTTPError(
            "https://api.github.test", status, "error", {}, io.BytesIO(json.dumps(body).encode()),
        )

    def test_retries_temporary_gateway_failure(self):
        request = urllib.request.Request("https://api.github.test/value")
        with patch.object(github_read.urllib.request, "urlopen", side_effect=[
                self.http_error(504, {"message": "timeout"}), self.response({"ok": True}),
        ]) as urlopen, patch.object(github_read.time, "sleep") as sleep:
            status, data = github_read.json_request(request)
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_rejects_write_requests(self):
        request = urllib.request.Request("https://api.github.test/value", data=b"{}", method="POST")
        with self.assertRaisesRegex(ValueError, "only accepts GET"):
            github_read.json_request(request)

    def test_returns_non_retryable_error_immediately(self):
        request = urllib.request.Request("https://api.github.test/value")
        with patch.object(github_read.urllib.request, "urlopen", side_effect=self.http_error(403, {"message": "forbidden"})) as urlopen, \
                patch.object(github_read.time, "sleep") as sleep:
            status, data = github_read.json_request(request)
        self.assertEqual((status, data), (403, {"message": "forbidden"}))
        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_or_fail_reports_exhausted_retryable_status(self):
        request = urllib.request.Request("https://api.github.test/value")
        failures = [self.http_error(504, {"message": "timeout"}) for _index in range(2)]
        with patch.object(github_read.urllib.request, "urlopen", side_effect=failures), \
                patch.object(github_read.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "lookup failed after 2 attempts: HTTP 504"):
                github_read.json_request_or_fail(request, "lookup", attempts=2)

    def test_retries_truncated_json_response(self):
        broken = MagicMock()
        broken.__enter__.return_value.status = 200
        broken.__enter__.return_value.read.return_value = b'{"partial"'
        with patch.object(github_read.urllib.request, "urlopen", side_effect=[broken, self.response({"ok": True})]), \
                patch.object(github_read.time, "sleep") as sleep:
            self.assertEqual(github_read.json_request(urllib.request.Request("https://api.github.test/value")), (200, {"ok": True}))
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
