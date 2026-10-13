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

    def test_repository_revision_drift_does_not_rebuild_good_mapping(self):
        item = self.item("new")
        key = item["key"]
        ready = {key: {"key": key, "source_revision": "old", "status": "ready",
                       "new_path": "derived/old/document.pdf", "derived_sha256": "a" * 64,
                       "derived_bytes": 12}}
        self.assertEqual(djvu.pending_records([item], ready), [])
        self.assertEqual(djvu.pending_records([item], ready, force=True), [item])
        self.assertEqual(djvu.pending_records([item], {}, force=True), [item])
        with self.assertRaisesRegex(ValueError, "exact --repo and --path"):
            import unittest.mock
            with unittest.mock.patch.object(djvu, "args", return_value=type("Args", (), {
                    "limit": 0, "repo": "", "path": "", "force": True})()):
                # Exercise the CLI guard before it touches credentials or remote indexes.
                djvu.main()

    def test_failed_entry_requires_explicit_retry_when_revision_is_unchanged(self):
        item = self.item("new")
        key = item["key"]
        failed = {key: {"key": key, "source_revision": "new", "status": "failed"}}
        self.assertEqual(djvu.pending_records([item], failed), [])
        self.assertEqual(djvu.pending_records([item], failed, retry_failed=True), [item])
        failed[key]["source_revision"] = "old"
        self.assertEqual(djvu.pending_records([item], failed), [item])

    def test_failure_checkpoint_is_explicit_and_attempt_counted(self):
        item = self.item()
        value = djvu.failure_entry(item, RuntimeError("bad source"), {"attempts": 2})
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["attempts"], 3)
        self.assertIn("bad source", value["error"])

    def test_failed_replacement_retains_the_last_good_mapping(self):
        item = self.item("new")
        previous = {"key": item["key"], "source_revision": "old", "status": "ready",
                    "new_path": "derived/old/document.pdf", "derived_sha256": "a" * 64,
                    "derived_bytes": 12}
        value = djvu.failure_entry(item, RuntimeError("temporary"), previous)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["new_path"], previous["new_path"])
        self.assertEqual(value["last_attempt_source_revision"], "new")

    def test_processing_identity_is_stable(self):
        self.assertEqual(djvu.processing_path("key", "revision", "a" * 64),
                         djvu.processing_path("key", "revision", "a" * 64))
        self.assertNotEqual(djvu.processing_path("key", "revision", "a" * 64),
                            djvu.processing_path("key", "revision", "b" * 64))

    def test_producer_workflows_release_writer_lock_before_unify_runs(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        for name in ("reader-djvu-weekly.yml", "reader-office-chapters.yml"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertIn('group: reader-sidecar', source)
            self.assertIn('operation=unify -f apply=true', source)
            self.assertNotIn('gh run watch', source)


if __name__ == "__main__":
    unittest.main()
