"""The live S3 probe can only touch its own random test key."""
import unittest
from unittest.mock import Mock

from scripts import search_bucket_probe as subject
from scripts.reader_bucket_store import S3NotFound


class Conflict(Exception):
    def __init__(self):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": 412}, "Error": {"Code": "PreconditionFailed"}}
        super().__init__("precondition failed")


class FakeClient:
    def __init__(self):
        self.raw = None
        self.etag = None
        self.requests = []

    def put_object(self, **kwargs):
        self.requests.append(("put", kwargs))
        if kwargs.get("IfNoneMatch") == "*" and self.raw is not None:
            raise Conflict()
        if "IfMatch" in kwargs and kwargs["IfMatch"] != self.etag:
            raise Conflict()
        self.raw = kwargs["Body"]
        self.etag = f'"version-{len(self.requests)}"'
        return {"ETag": self.etag}

    def delete_object(self, **kwargs):
        self.requests.append(("delete", kwargs))
        if kwargs.get("IfMatch") != self.etag:
            raise Conflict()
        self.raw = None
        self.etag = None


class FakeStore:
    def __init__(self):
        self.client = FakeClient()
        self.read_etags = {}

    def _location(self, bucket):
        if bucket != subject.BUCKET:
            raise AssertionError("wrong bucket")
        return ("vomebook", "search-data-v1")

    def _client(self, namespace):
        if namespace != "vomebook":
            raise AssertionError("wrong namespace")
        return self.client

    def read_bytes(self, bucket, key):
        if self.client.raw is None:
            raise S3NotFound(key)
        self.read_etags[(bucket, key)] = self.client.etag
        return self.client.raw


class FakeHub:
    def __init__(self, store):
        self.store = store

    def read_bytes(self, bucket, key):
        return self.store.read_bytes(bucket, key)


class SearchBucketProbeTests(unittest.TestCase):
    def test_probe_checks_both_preconditions_hub_s3_and_conditional_delete(self):
        store = FakeStore()
        result = subject.probe(store, FakeHub(store), "a" * 32)
        self.assertTrue(result["if_none_match_verified"])
        self.assertTrue(result["if_match_verified"])
        self.assertTrue(result["stale_match_rejected"])
        self.assertTrue(result["conditional_delete_verified"])
        self.assertFalse(result["production_pointer_written"])
        self.assertFalse(result["external_writers_isolated"])
        self.assertEqual(len(store.client.requests), 5)
        for method, call in store.client.requests:
            self.assertEqual(call["Bucket"], "search-data-v1")
            self.assertEqual(call["Key"], "probes/s3-conditional-" + "a" * 32 + ".json")
            self.assertNotEqual(call["Key"], "published/current.json")
        self.assertEqual(store.client.requests[-1][1]["IfMatch"], '"version-3"')
        self.assertIsNone(store.client.raw)

    def test_existing_probe_and_invalid_id_are_rejected(self):
        store = FakeStore()
        store.client.raw = b"other"
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            subject.probe(store, FakeHub(store), "a" * 32)
        self.assertEqual(store.client.requests, [])
        with self.assertRaises(ValueError):
            subject.probe(store, FakeHub(store), "../published/current.json")

    def test_unknown_write_outcome_is_not_retried_or_deleted(self):
        store = FakeStore()
        store.client.put_object = Mock(side_effect=TimeoutError("unknown outcome"))
        with self.assertRaises(TimeoutError):
            subject.probe(store, FakeHub(store), "b" * 32)
        store.client.put_object.assert_called_once()
        self.assertEqual(store.client.requests, [])

    def test_default_cli_does_not_open_a_store(self):
        with unittest.mock.patch.object(subject, "S3BucketStore", side_effect=AssertionError("must not open")):
            self.assertEqual(subject.main([]), 0)


if __name__ == "__main__":
    unittest.main()
