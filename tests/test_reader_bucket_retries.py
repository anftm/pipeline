import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import reader_bucket


class ReaderBucketRetryTests(unittest.TestCase):
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
