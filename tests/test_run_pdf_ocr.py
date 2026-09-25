import unittest
from unittest.mock import patch

import requests
from huggingface_hub.errors import HfHubHTTPError

from scripts import run_pdf_ocr


class RunPdfOcrTests(unittest.TestCase):
    def test_bucket_sync_honors_retry_after(self):
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "17"
        response.request = requests.Request(
            "POST", "https://huggingface.co/api/buckets/vomebook/pdf-pages/tree"
        ).prepare()
        error = HfHubHTTPError("rate limited", response=response)

        with patch.object(run_pdf_ocr, "sync_bucket", side_effect=[error, None]) as sync, \
                patch.object(run_pdf_ocr.time, "sleep") as sleep:
            run_pdf_ocr._sync_bucket_with_retry("/tmp/book", "hf://buckets/vomebook/pdf-pages", "token")

        self.assertEqual(sync.call_count, 2)
        sleep.assert_called_once_with(17)

    def test_bucket_sync_retries_transient_server_error(self):
        response = requests.Response()
        response.status_code = 503
        response.request = requests.Request(
            "POST", "https://huggingface.co/api/buckets/vomebook/pdf-pages/tree"
        ).prepare()
        error = HfHubHTTPError("busy", response=response)

        with patch.object(run_pdf_ocr, "sync_bucket", side_effect=[error, None]), \
                patch.object(run_pdf_ocr.time, "sleep") as sleep:
            run_pdf_ocr._sync_bucket_with_retry("/tmp/book", "hf://buckets/vomebook/pdf-pages", "token")

        sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
