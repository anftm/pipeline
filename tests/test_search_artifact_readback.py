"""Real-bucket behavior is never inferred from a mock S3 response."""
import hashlib
import unittest
from unittest.mock import Mock

from scripts.search_artifact_s3 import S3SearchArtifactStore


class LostResponseReadbackTests(unittest.TestCase):
    def test_adapter_does_not_retry_content_create_after_unknown_result(self):
        raw = Mock()
        raw._location.return_value = ("vomebook", "search-data-v1")
        raw._client.return_value.put_object.side_effect = TimeoutError("connection lost")
        data = b"candidate"
        digest = hashlib.sha256(data).hexdigest()
        key = f"objects/sha256/{digest[:2]}/{digest}"
        with self.assertRaises(TimeoutError):
            S3SearchArtifactStore(raw).create_immutable(key, data)
        raw._client.return_value.put_object.assert_called_once()
        self.assertEqual(raw._client.return_value.put_object.call_args.kwargs["IfNoneMatch"], "*")


if __name__ == "__main__":
    unittest.main()
