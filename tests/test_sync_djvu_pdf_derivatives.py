import unittest

from scripts import sync_djvu_pdf_derivatives as djvu


class DjvuCheckpointTests(unittest.TestCase):
    def item(self, revision="rev"):
        return {"key": "VoiceOfML/Test\0book.djvu", "repo": "VoiceOfML/Test",
                "path": "book.djvu", "revision": revision}

    def test_existing_ready_entry_is_a_checkpoint_even_from_old_manifest_schema(self):
        item = self.item()
        existing = {item["key"]: {"key": item["key"], "source_revision": "rev",
                                   "new_path": "derived/old/document.pdf", "derived_sha256": "a" * 64,
                                   "derived_bytes": 12}}
        self.assertEqual(djvu.pending_records([item], existing), [])

    def test_changed_revision_requeues_but_failed_entry_requires_explicit_retry(self):
        item = self.item("new")
        key = item["key"]
        failed = {key: {"key": key, "source_revision": "new", "status": "failed"}}
        self.assertEqual(djvu.pending_records([item], failed), [])
        self.assertEqual(djvu.pending_records([item], failed, retry_failed=True), [item])
        old = {key: {"key": key, "source_revision": "old", "status": "ready",
                     "new_path": "derived/old/document.pdf", "derived_sha256": "a" * 64,
                     "derived_bytes": 12}}
        self.assertEqual(djvu.pending_records([item], old), [item])

    def test_failure_checkpoint_is_explicit_and_attempt_counted(self):
        item = self.item()
        value = djvu.failure_entry(item, RuntimeError("bad source"), {"attempts": 2})
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["attempts"], 3)
        self.assertIn("bad source", value["error"])

    def test_processing_identity_is_stable(self):
        self.assertEqual(djvu.processing_path("key", "revision", "a" * 64),
                         djvu.processing_path("key", "revision", "a" * 64))
        self.assertNotEqual(djvu.processing_path("key", "revision", "a" * 64),
                            djvu.processing_path("key", "revision", "b" * 64))


if __name__ == "__main__":
    unittest.main()
