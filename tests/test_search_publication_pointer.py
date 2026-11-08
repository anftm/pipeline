"""A desired-generation pointer is single-object CAS, not a site receipt."""
import unittest
from unittest.mock import Mock

from scripts import search_publication_pointer as pointer
from scripts.reader_bucket_store import S3NotFound


class MemoryPointer:
    def __init__(self, raw=None):
        self.raw = raw
        self.etag = '"version"' if raw is not None else None
        self.calls = 0
        self.lose_response = False

    def read_for_update(self):
        return self.raw, self.etag

    def compare_and_swap(self, *, expected_etag, raw):
        self.calls += 1
        if self.etag != expected_etag:
            raise RuntimeError("conflict")
        self.raw, self.etag = raw, '"new"'
        if self.lose_response:
            raise TimeoutError("response lost after commit")


class SearchPublicationPointerTests(unittest.TestCase):
    def test_first_pointer_then_transition_preserves_expected_previous(self):
        store = MemoryPointer()
        a, b = "a" * 64, "b" * 64
        first = pointer.apply_pointer(store, desired=a, expected=None, transition="1" * 32)
        self.assertEqual(first["status"], "written")
        self.assertFalse(first["consumers_verified"])
        second = pointer.apply_pointer(store, desired=b, expected=a, transition="2" * 32)
        self.assertEqual(second["previous"], a)
        self.assertEqual(pointer.decode_pointer(store.raw)["desired"], b)
        self.assertEqual(pointer.decode_pointer(store.raw)["previous"], a)

    def test_stale_parent_and_same_generation_do_not_write(self):
        store = MemoryPointer(pointer.make_pointer("a" * 64, None, "1" * 32))
        with self.assertRaisesRegex(ValueError, "parent changed"):
            pointer.apply_pointer(store, desired="b" * 64, expected=None, transition="2" * 32)
        with self.assertRaisesRegex(ValueError, "already current"):
            pointer.apply_pointer(store, desired="a" * 64, expected="a" * 64, transition="2" * 32)
        self.assertEqual(store.calls, 0)

    def test_full_expected_parent_detects_same_generation_different_transition(self):
        old = pointer.make_pointer("a" * 64, None, "1" * 32)
        store = MemoryPointer(pointer.make_pointer("a" * 64, None, "2" * 32))
        with self.assertRaisesRegex(ValueError, "full pointer identity changed"):
            pointer.apply_pointer(store, desired="b" * 64, expected="a" * 64,
                                  transition="3" * 32, expected_pointer=old)
        self.assertEqual(store.calls, 0)
        result = pointer.apply_pointer(store, desired="b" * 64, expected="a" * 64,
                                       transition="3" * 32, expected_pointer=store.raw)
        self.assertEqual(result["status"], "written")
        self.assertEqual(store.calls, 1)

    def test_missing_validator_cannot_turn_existing_pointer_into_create(self):
        store = MemoryPointer(pointer.make_pointer("a" * 64, None, "1" * 32))
        store.etag = None
        with self.assertRaisesRegex(ValueError, "validator are inconsistent"):
            pointer.apply_pointer(store, desired="b" * 64, expected="a" * 64, transition="2" * 32)
        self.assertEqual(store.calls, 0)

    def test_lost_response_is_read_back_once_and_never_retried(self):
        store = MemoryPointer()
        store.lose_response = True
        result = pointer.apply_pointer(store, desired="a" * 64, expected=None, transition="1" * 32)
        self.assertEqual(result["status"], "written-response-unknown")
        self.assertEqual(store.calls, 1)
        self.assertFalse(result["consumers_verified"])

    def test_invalid_pointer_and_unknown_fields_are_rejected(self):
        malformed = (b'{"version":1,"kind":"search-publication-pointer",'
                     b'"desired":"bad","previous":null,"transition":"x"}')
        for raw in (b"{}", b"not json", malformed):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pointer.decode_pointer(raw)
        with self.assertRaises(ValueError):
            pointer.make_pointer("a" * 64, None, "bad")

    def test_real_adapter_uses_only_exact_pointer_and_conditions(self):
        backing = Mock()
        backing._location.return_value = ("vomebook", "search-data-v1")
        backing._client.return_value = Mock()
        backing.read_bytes.side_effect = S3NotFound("missing")
        store = pointer.S3SearchPointerStore(backing)
        self.assertEqual(store.read_for_update(), (None, None))
        raw = pointer.make_pointer("a" * 64, None, "1" * 32)
        store.compare_and_swap(expected_etag=None, raw=raw)
        backing._client.return_value.put_object.assert_called_once_with(
            Bucket="search-data-v1", Key="published/current.json", Body=raw,
            ContentType="application/json", IfNoneMatch="*")
        backing._client.return_value.reset_mock()
        store.compare_and_swap(expected_etag='"actual"', raw=raw)
        self.assertEqual(backing._client.return_value.put_object.call_args.kwargs["IfMatch"], '"actual"')
        with self.assertRaises(ValueError):
            store.compare_and_swap(expected_etag='W/"weak"', raw=raw)


if __name__ == "__main__":
    unittest.main()
