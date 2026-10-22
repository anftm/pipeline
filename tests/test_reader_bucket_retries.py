import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import reader_bucket, shared


class ReaderBucketRetryTests(unittest.TestCase):
    def test_serial_upload_resets_poisoned_xet_session_and_retries_identical_batch(self):
        batch = [(b"same bytes", "reader-index/processing/test.json")]
        original = ConnectionError("HTTP status client error (429 Too Many Requests), domain: xet-write-token")
        stale = RuntimeError("Previous task error: " + str(original))
        poisoned, calls = False, []

        def upload(bucket, *, add, token):
            nonlocal poisoned
            calls.append((bucket, add, token))
            if len(calls) == 1:
                poisoned = True
                raise original
            if poisoned:
                raise stale

        def reset():
            nonlocal poisoned
            poisoned = False

        with patch("huggingface_hub.batch_bucket_files", side_effect=upload), \
                patch("huggingface_hub.utils._xet.abort_xet_session", side_effect=reset) as abort, \
                patch.object(shared.time, "sleep") as sleep, \
                patch.object(shared.random, "uniform", return_value=0):
            shared.batch_bucket_files_with_retry("vomebook/reader-assets-v2", batch, "fixture",
                                                 reset_xet_on_retry=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIs(calls[0][1], batch)
        abort.assert_called_once_with()
        sleep.assert_called_once_with(60)

    def test_upload_cooldown_honors_headers_body_and_opaque_429(self):
        error = RuntimeError("Previous task error: HTTP status client error (429 Too Many Requests), domain: xet-write-token")
        self.assertEqual([shared.bucket_upload_retry_delay(error, i) for i in range(6)],
                         [60, 120, 240, 300, 300, 300])
        error.response = Mock(status_code=429, headers={"Retry-After": "23.5"}, text="")
        self.assertEqual(shared.bucket_upload_retry_delay(error, 0), 24)
        error.response.headers = {"Retry-After": "Thu, 01 Jan 1970 00:01:30 GMT"}
        with patch.object(shared.time, "time", return_value=0):
            self.assertEqual(shared.bucket_upload_retry_delay(error, 0), 90)
        error.response.headers = {"Retry-After": "NaN"}
        error.response.text = "Rate limited. Retry after 83 seconds (0 requests remaining)."
        self.assertEqual(shared.bucket_upload_retry_delay(error, 0), 83)
        self.assertEqual(shared.bucket_upload_retry_delay(TimeoutError("timeout"), 7), 120)

    def test_shared_upload_retry_is_bounded_and_does_not_reset_other_callers(self):
        error = RuntimeError("Previous task error: HTTP status client error (429 Too Many Requests), domain: xet-write-token")
        with patch("huggingface_hub.batch_bucket_files", side_effect=error) as upload, \
                patch("huggingface_hub.utils._xet.abort_xet_session") as abort, \
                patch.object(shared.time, "sleep") as sleep, \
                patch.object(shared.random, "uniform", return_value=0):
            with self.assertRaises(RuntimeError) as raised:
                shared.batch_bucket_files_with_retry("vomebook/reader-assets-v2", [(b"same", "test")],
                                                     None, max_attempts=3)
        self.assertIs(raised.exception, error)
        self.assertEqual(upload.call_count, 3)
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(60), unittest.mock.call(120)])
        abort.assert_not_called()

    def test_upload_integrity_and_auth_errors_fail_without_cooldown_or_reset(self):
        errors = [ValueError("invalid immutable payload"),
                  RuntimeError("HTTP status client error (403 Forbidden), domain: xet-write-token")]
        for error in errors:
            with self.subTest(error=str(error)), \
                    patch("huggingface_hub.batch_bucket_files", side_effect=error) as upload, \
                    patch("huggingface_hub.utils._xet.abort_xet_session") as abort, \
                    patch.object(shared.time, "sleep") as sleep:
                with self.assertRaises(type(error)):
                    shared.batch_bucket_files_with_retry("vomebook/reader-assets-v2", [(b"same", "test")],
                                                         None, reset_xet_on_retry=True)
            upload.assert_called_once()
            abort.assert_not_called()
            sleep.assert_not_called()

    def test_index_upload_retries_connection_style_xet_429(self):
        response = Mock(status_code=429, headers={"Retry-After": "4"})
        error = ConnectionError(
            "Network error: HTTP status client error (429 Too Many Requests), "
            "domain: https://huggingface.co/api/buckets/vomebook/reader-assets-v2/xet-write-token")
        error.response = response
        local = Path("/tmp/reader-bucket-retry-test.json")
        local.write_text("{}", encoding="utf-8")
        try:
            with patch.object(reader_bucket, "batch_bucket_files",
                              side_effect=[error, None]), \
                    patch.object(reader_bucket.time, "sleep") as sleep:
                reader_bucket._publish_additions([(str(local), "reader-index/test.json")], "token")
            sleep.assert_called_once()
            self.assertGreaterEqual(sleep.call_args.args[0], 4)
        finally:
            local.unlink(missing_ok=True)

    def test_resolver_retries_retry_after_before_failing(self):
        response = Mock(status_code=429, headers={"Retry-After": "6"})
        response.content = b""
        ok = Mock(status_code=200, content=b"complete")
        session = Mock()
        session.get.side_effect = [response, ok]
        with patch.object(reader_bucket, "get_session", return_value=session), \
                patch.object(reader_bucket, "hf_raise_for_status", return_value=None), \
                patch.object(reader_bucket.time, "sleep") as sleep:
            self.assertEqual(reader_bucket.read_bytes("reader-index/test.json"), b"complete")
        sleep.assert_called_once()
        self.assertGreaterEqual(sleep.call_args.args[0], 6)


if __name__ == "__main__":
    unittest.main()
