"""Conditional S3 staging never writes outside immutable search paths."""
import hashlib
import unittest
from unittest.mock import Mock

from scripts.reader_bucket_store import S3NotFound
from scripts.search_artifact_s3 import BUCKET, S3SearchArtifactStore


class FakeS3Error(Exception):
    def __init__(self, status, code):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": code}}
        super().__init__(code)


class S3SearchArtifactTests(unittest.TestCase):
    def setUp(self):
        self.raw = Mock()
        self.raw._location.return_value = ("vomebook", "search-data-v1")
        self.raw._client.return_value = Mock()
        self.store = S3SearchArtifactStore(self.raw)
        digest = hashlib.sha256(b"content").hexdigest()
        self.key = f"objects/sha256/{digest[:2]}/{digest}"
        self.manifest = "generations/" + "b" * 64 + "/manifest.json"

    def test_exact_object_create_is_conditional_and_content_type_safe(self):
        self.store.create_immutable(self.key, b"content")
        self.raw._client.return_value.put_object.assert_called_once_with(
            Bucket="search-data-v1", Key=self.key, Body=b"content",
            ContentType="application/octet-stream", IfNoneMatch="*")
        self.raw._client.reset_mock()
        self.store.create_immutable(self.manifest, b"{}")
        self.raw._client.return_value.put_object.assert_called_once_with(
            Bucket="search-data-v1", Key=self.manifest, Body=b"{}",
            ContentType="application/json", IfNoneMatch="*")

    def test_mutable_roots_and_malformed_hashes_are_rejected(self):
        for key in ("published/current.json", "../objects/sha256/aa/" + "a" * 64,
                    "objects/sha256/bb/" + "a" * 64, "generations/" + "a" * 64 + "/other.json"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.create_immutable(key, b"data")
        self.raw._client.assert_not_called()

    def test_successful_reads_and_not_found_are_distinct_from_forbidden(self):
        self.raw.read_bytes.return_value = b"verified"
        self.assertEqual(self.store.read_optional(self.key), b"verified")
        self.raw.read_bytes.assert_called_once_with(BUCKET, self.key)
        self.raw.read_bytes.side_effect = FakeS3Error(404, "NoSuchKey")
        self.assertIsNone(self.store.read_optional(self.key))
        self.raw.read_bytes.side_effect = S3NotFound("missing")
        self.assertIsNone(self.store.read_optional(self.key))
        self.raw.read_bytes.side_effect = FakeS3Error(403, "AccessDenied")
        with self.assertRaises(FakeS3Error):
            self.store.read_optional(self.key)

    def test_unknown_write_failure_propagates_without_retry(self):
        self.raw._client.return_value.put_object.side_effect = TimeoutError("lost response")
        with self.assertRaises(TimeoutError):
            self.store.create_immutable(self.key, b"content")
        self.raw._client.return_value.put_object.assert_called_once()

    def test_no_empty_unbounded_or_misaddressed_content(self):
        with self.assertRaises(ValueError):
            self.store.create_immutable(self.key, b"")
        with self.assertRaises(ValueError):
            self.store.create_immutable(self.key, "not bytes")
        with self.assertRaisesRegex(ValueError, "content address"):
            self.store.create_immutable(self.key, b"wrong bytes")
        self.raw._client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
