import json
import io
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import nullcontext, redirect_stdout
from unittest.mock import MagicMock, patch

from app import bucket_snapshot
from app.config import INDEX_VERSION


class BucketSnapshotTests(unittest.TestCase):
    revisions = {archive_id: "1" * 40 for archive_id in range(32)}

    def test_manifest_rejects_malformed_nested_shapes(self):
        metadata = bucket_snapshot.desired_metadata("0" * 64, self.revisions)
        valid = {
            "schema": 3, "snapshot": "bha-1-aaaaaaaa", "generation": "bha-1-aaaaaaaa",
            "physical_index": "article-build", "index_metadata": metadata,
            "archive_commits": {str(key): value for key, value in self.revisions.items()},
            "sidecars": {key: {"filename": path.name} for key, path in bucket_snapshot.SIDECARS.items()},
        }
        invalid = [
            {"index_metadata": []},
            {"index_metadata": {**metadata, "archive_revisions": []}},
            {"archive_commits": []},
            {"sidecars": []},
            {"sidecars": {key: None for key in bucket_snapshot.SIDECARS}},
            {"generation": "../outside"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            path.write_text(json.dumps(valid))
            self.assertIsNotNone(bucket_snapshot.read_manifest(path.parent))
            for change in invalid:
                with self.subTest(change=change):
                    path.write_text(json.dumps({**valid, **change}))
                    self.assertIsNone(bucket_snapshot.read_manifest(path.parent))

    def create_sidecars(self, data_root: Path) -> tuple[Path, Path]:
        docs = data_root / "docs.sqlite"
        facets = data_root / "facets.sqlite"
        with sqlite3.connect(docs) as conn:
            conn.execute("CREATE TABLE docs (doc_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO docs VALUES ('doc-1')")
        with sqlite3.connect(facets) as conn:
            conn.execute("CREATE TABLE facets (kind TEXT, name TEXT)")
            conn.execute("INSERT INTO facets VALUES ('source', '刊物')")
        metadata = bucket_snapshot.desired_metadata("0" * 64, self.revisions)
        with sqlite3.connect(docs) as conn:
            conn.execute("CREATE TABLE build_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO build_meta VALUES ('index_metadata', ?)", (json.dumps(metadata, sort_keys=True),))
        with sqlite3.connect(facets) as conn:
            conn.execute("CREATE TABLE build_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO build_meta VALUES ('index_metadata', ?)", (json.dumps(metadata, sort_keys=True),))
        return docs, facets

    def test_publish_then_restore_validates_elasticsearch_and_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root = root / "data"
            data_root.mkdir()
            docs, facets = self.create_sidecars(data_root)
            bucket = root / "bucket"
            bucket.mkdir()
            old_generation = bucket / "generations" / "bha-0-bbbbbbbb"
            old_generation.mkdir(parents=True)
            client = MagicMock()
            client.get.return_value = {"_source": bucket_snapshot.desired_metadata("0" * 64, self.revisions)}
            client.count.return_value = {"count": 1}
            client.indices.exists.return_value = False
            remote_commits = dict(self.revisions)
            remote_commits[0] = "2" * 40

            def commits(*, remote=False, **_):
                return remote_commits if remote else self.revisions

            with (
                patch.object(bucket_snapshot, "Elasticsearch", return_value=client),
                patch.object(bucket_snapshot, "DATA_ROOT", data_root),
                patch.object(bucket_snapshot, "DOC_DB", docs),
                patch.object(bucket_snapshot, "FACET_DB", facets),
                patch.object(bucket_snapshot, "SIDECARS", {"docs": docs, "facets": facets}),
                patch.object(bucket_snapshot, "parsed_corpus_fingerprint", return_value="0" * 64),
                patch.object(bucket_snapshot, "parsed_archive_revisions", return_value=self.revisions),
                patch.object(bucket_snapshot, "parsed_archive_commits", side_effect=commits),
                patch.object(bucket_snapshot, "parsed_archive_tree_revisions", return_value={0: "1" * 40}),
                patch.object(bucket_snapshot, "physical_index", return_value="article-build"),
                patch.object(bucket_snapshot, "local_repository", return_value=data_root / "es-repository"),
                patch.object(bucket_snapshot, "snapshot_publish_lock", return_value=nullcontext()),
                patch.object(bucket_snapshot, "serving_lock", return_value=nullcontext()),
            ):
                local_repository = data_root / "es-repository"
                local_repository.mkdir()
                (local_repository / "index-0").write_text("repository", encoding="utf-8")
                bucket_snapshot.publish(bucket)
                manifest = bucket_snapshot.read_manifest(bucket)
                self.assertIsNotNone(manifest)
                self.assertEqual(manifest["document_count"], 1)
                self.assertEqual(manifest["physical_index"], "article-build")
                self.assertTrue(old_generation.exists())
                docs.unlink()
                facets.unlink()
                sidecar_started = threading.Event()
                release_sidecar = threading.Event()
                original_prepare = bucket_snapshot.prepare_restored_sidecars

                def delayed_prepare(*args):
                    sidecar_started.set()
                    release_sidecar.wait(2)
                    return original_prepare(*args)

                def restore_while_sidecar_is_running(**_kwargs):
                    self.assertTrue(sidecar_started.wait(1))
                    release_sidecar.set()

                client.snapshot.restore.side_effect = restore_while_sidecar_is_running
                with patch.object(bucket_snapshot, "prepare_restored_sidecars", side_effect=delayed_prepare):
                    self.assertTrue(bucket_snapshot.restore(bucket))
                status = bucket_snapshot.snapshot_status()
                self.assertEqual(status["state"], "restored")
                self.assertEqual(set(status["stage_seconds"]), {
                    "prepare_local_repository", "copy_snapshot_repository",
                    "register_snapshot_repository", "configure_recovery",
                    "restore_elasticsearch", "validate_elasticsearch",
                    "wait_sidecars", "activate_sidecars",
                    "validate_sidecars", "cleanup_local_repository",
                })
                self.assertIn("prepare_sidecars", status["parallel_seconds"])
                self.assertEqual(status["recovery_max_bytes_per_sec"], "160mb")
                self.assertTrue(status["recovery_limit_applied"])
                self.assertEqual(bucket_snapshot.read_manifest(bucket)["archive_commits"]["0"], "2" * 40)

            self.assertTrue(docs.exists())
            self.assertTrue(facets.exists())
            client.snapshot.delete_repository.assert_called_once_with(name=bucket_snapshot.REPOSITORY)
            self.assertTrue(client.snapshot.create_repository.call_args.kwargs["repository"]["settings"]["location"].endswith("es-repository"))
            self.assertEqual(client.snapshot.create.call_args.kwargs["indices"], "article-build")
            self.assertEqual(client.snapshot.restore.call_args.kwargs["indices"], "article-build")
            recovery_calls = [call.kwargs["transient"] for call in client.cluster.put_settings.call_args_list]
            self.assertEqual(recovery_calls, [
                {"indices.recovery.max_bytes_per_sec": "160mb"},
                {"indices.recovery.max_bytes_per_sec": None},
            ])

    def test_restore_without_manifest_falls_back_to_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(bucket_snapshot.restore(Path(directory)))

    def test_publish_serializes_publishers_and_uses_shared_serving_lock(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bucket_snapshot, "DATA_ROOT", Path(directory)), \
                patch.object(bucket_snapshot, "snapshot_publish_lock", return_value=nullcontext()) as publish_lock, \
                patch.object(bucket_snapshot, "serving_lock", return_value=nullcontext()) as serving, \
                patch.object(bucket_snapshot, "_publish") as publish:
            bucket = Path(directory) / "bucket"
            bucket_snapshot.publish(bucket)
            self.assertFalse(bucket_snapshot.publish_marker_path().exists())
        publish_lock.assert_called_once_with()
        serving.assert_called_once_with()
        publish.assert_called_once_with(bucket)

    def test_failed_publication_cleans_up_before_releasing_publish_lock(self):
        events = []

        class RecordingLock:
            def __enter__(self):
                events.append("lock-enter")

            def __exit__(self, *_args):
                events.append("lock-exit")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bucket_snapshot, "DATA_ROOT", Path(directory)), \
                patch.object(bucket_snapshot, "snapshot_publish_lock", return_value=RecordingLock()), \
                patch.object(bucket_snapshot, "serving_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "_publish", side_effect=RuntimeError("failed")), \
                patch.object(bucket_snapshot, "cleanup_failed_publication", side_effect=lambda _bucket: events.append("cleanup")), \
                patch.object(bucket_snapshot, "write_snapshot_status"):
            with self.assertRaises(RuntimeError):
                bucket_snapshot.publish(Path("/bucket"))
            self.assertTrue(bucket_snapshot.publish_marker_path().exists())
        self.assertEqual(events, ["lock-enter", "cleanup", "lock-exit"])

    def test_failed_publication_keeps_serving_lock_during_cleanup(self):
        events = []
        class RecordingLock:
            def __enter__(self):
                events.append("serving-enter")
            def __exit__(self, *_args):
                events.append("serving-exit")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bucket_snapshot, "DATA_ROOT", Path(directory)), \
                patch.object(bucket_snapshot, "snapshot_publish_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "serving_lock", return_value=RecordingLock()), \
                patch.object(bucket_snapshot, "_publish", side_effect=RuntimeError("failed")), \
                patch.object(bucket_snapshot, "cleanup_failed_publication", side_effect=lambda _bucket: events.append("cleanup")), \
                patch.object(bucket_snapshot, "write_snapshot_status"):
            with self.assertRaises(RuntimeError):
                bucket_snapshot.publish(Path("/bucket"))
        self.assertEqual(events, ["serving-enter", "cleanup", "serving-exit"])

    def test_incomplete_marker_requires_review_even_when_status_is_restored(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bucket_snapshot, "DATA_ROOT", Path(directory)), \
                patch.object(bucket_snapshot, "snapshot_publish_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "serving_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "snapshot_status", return_value={"state": "restored"}), \
                patch.object(bucket_snapshot, "_publish") as publish, \
                patch.object(bucket_snapshot, "cleanup_failed_publication") as cleanup, \
                patch.object(bucket_snapshot, "write_snapshot_status"):
            marker = bucket_snapshot.create_publish_marker()
            with self.assertRaises(bucket_snapshot.PublicationIncomplete):
                bucket_snapshot.publish(Path("/bucket"))
            self.assertTrue(marker.exists())
        publish.assert_not_called()
        cleanup.assert_not_called()

    def test_interrupted_publication_leaves_marker_across_restore_status_change(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bucket_snapshot, "DATA_ROOT", Path(directory)), \
                patch.object(bucket_snapshot, "snapshot_publish_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "serving_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "_publish", side_effect=SystemExit("synthetic crash")), \
                patch.object(bucket_snapshot, "write_snapshot_status"):
            with self.assertRaises(SystemExit):
                bucket_snapshot.publish(Path("/bucket"))
            self.assertTrue(bucket_snapshot.publish_marker_path().exists())
            with patch.object(bucket_snapshot, "snapshot_status", return_value={"state": "restored"}), \
                    patch.object(bucket_snapshot, "_publish") as publish:
                with self.assertRaises(bucket_snapshot.PublicationIncomplete):
                    bucket_snapshot.publish(Path("/bucket"))
            publish.assert_not_called()

    def test_sigkill_during_publish_blocks_new_process_using_same_data_root(self):
        # This checks a real process death and flock release on a local test
        # filesystem, not survival of the Space container or bucket mount.
        program = r'''
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch
from app import bucket_snapshot, storage_lock
root = Path(sys.argv[1])
def interrupted(_bucket):
    os.kill(os.getpid(), signal.SIGKILL)
with patch.object(bucket_snapshot, "DATA_ROOT", root), \
        patch.object(storage_lock, "SNAPSHOT_PUBLISH_LOCK_PATH", root / "publish.lock"), \
        patch.object(storage_lock, "LOCK_PATH", root / "serving.lock"), \
        patch.object(bucket_snapshot, "_publish", side_effect=interrupted):
    bucket_snapshot.publish(root / "synthetic-bucket")
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = subprocess.run([sys.executable, "-B", "-c", program, str(root)],
                                   capture_output=True, check=False, timeout=15)
            self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr.decode(errors="replace"))
            with patch.object(bucket_snapshot, "DATA_ROOT", root), \
                    patch("app.storage_lock.SNAPSHOT_PUBLISH_LOCK_PATH", root / "publish.lock"), \
                    patch("app.storage_lock.LOCK_PATH", root / "serving.lock"), \
                    patch.object(bucket_snapshot, "_publish") as publish:
                bucket_snapshot.write_snapshot_status("restored", snapshot="bha-fixture")
                self.assertTrue(bucket_snapshot.publish_marker_path().is_file())
                with self.assertRaises(bucket_snapshot.PublicationIncomplete):
                    bucket_snapshot.publish(root / "synthetic-bucket")
                publish.assert_not_called()

    def test_direct_publish_concurrency_marker_blocks_second_without_changing_status(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bucket_snapshot, "DATA_ROOT", Path(directory)), \
                patch.object(bucket_snapshot, "snapshot_publish_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "serving_lock", return_value=nullcontext()), \
                patch.object(bucket_snapshot, "write_snapshot_status") as status, \
                patch.object(bucket_snapshot, "cleanup_failed_publication") as cleanup:
            def publishing(_bucket):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release publisher")
            def first():
                try:
                    bucket_snapshot.publish(Path(directory))
                except Exception as exc:
                    errors.append(exc)
            with patch.object(bucket_snapshot, "_publish", side_effect=publishing) as publish:
                thread = threading.Thread(target=first)
                thread.start()
                self.assertTrue(entered.wait(5))
                with self.assertRaises(bucket_snapshot.PublicationIncomplete):
                    bucket_snapshot.publish(Path(directory))
                self.assertTrue(bucket_snapshot.publish_marker_path().exists())
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(bucket_snapshot.publish_marker_path().exists())
        publish.assert_called_once()
        cleanup.assert_not_called()
        status.assert_not_called()

    def test_publish_delay_uses_manifest_creation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            bucket = Path(directory)
            (bucket / "current.json").write_text(json.dumps({"created_at": 950}), encoding="utf-8")
            with patch.object(bucket_snapshot.time, "time", return_value=1000):
                self.assertEqual(bucket_snapshot.publish_delay(bucket, 100), 50)
                self.assertFalse(bucket_snapshot.publish_due(bucket, 100))

    def test_failed_publication_cleanup_keeps_all_bucket_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bucket = root / "bucket"
            keep = bucket / "generations" / "bha-1-aaaaaaaa"
            orphan = bucket / "generations" / "bha-2-bbbbbbbb"
            keep.mkdir(parents=True)
            orphan.mkdir(parents=True)
            (orphan / "large.partial").write_text("partial", encoding="utf-8")
            (bucket / "current.json").write_text(json.dumps({"generation": keep.name}), encoding="utf-8")
            repository = root / "repository"
            repository.mkdir()
            (repository / "partial").write_text("partial", encoding="utf-8")
            with patch.object(bucket_snapshot, "local_repository", return_value=repository):
                bucket_snapshot.cleanup_failed_publication(bucket)
            self.assertTrue(keep.exists())
            self.assertTrue(orphan.exists())
            self.assertFalse((repository / "partial").exists())

    def test_failed_publication_with_missing_manifest_preserves_bucket_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bucket = root / "bucket"
            old = bucket / "generations" / "bha-1-aaaaaaaa"
            newer = bucket / "generations" / "bha-2-bbbbbbbb"
            old.mkdir(parents=True)
            newer.mkdir(parents=True)
            temporary = bucket / ".current.json.other-writer.tmp"
            temporary.write_text("synthetic", encoding="utf-8")
            legacy = bucket / "snapshots"
            legacy.mkdir()
            with patch.object(bucket_snapshot, "local_repository", return_value=root / "repository"):
                bucket_snapshot.cleanup_failed_publication(bucket)
            for path in (old, newer, temporary, legacy):
                self.assertTrue(path.exists())

    def test_link_or_copy_falls_back_when_hardlink_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_text("data", encoding="utf-8")
            with patch.object(bucket_snapshot.os, "link", side_effect=OSError("cross-device")):
                bucket_snapshot.link_or_copy(source, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "data")

    def test_restore_rejects_changed_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            bucket = Path(directory)
            (bucket / "current.json").write_text("{}", encoding="utf-8")
            manifest = {
                "schema": 3,
                "index_metadata": bucket_snapshot.desired_metadata("0" * 64, self.revisions),
                "archive_commits": {str(key): value for key, value in self.revisions.items()},
                "snapshot": "bha-1-aaaaaaaa",
                "generation": "bha-1-aaaaaaaa",
                "physical_index": "article-build",
                "source_fingerprint": "0" * 64,
                "sidecars": {"docs": {"filename": "docs.sqlite"}, "facets": {"filename": "facets.sqlite"}},
            }
            (bucket / "current.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(bucket_snapshot, "parsed_archive_commits", return_value={0: "2" * 40}), \
                    patch.object(bucket_snapshot, "parsed_archive_tree_revisions", return_value={0: "2" * 40}):
                self.assertFalse(bucket_snapshot.restore(bucket, allow_stale=False))

    def test_failed_restore_deletes_alias_and_physical_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bucket = root / "bucket"
            repository = bucket / "generations" / "bha-1-aaaaaaaa" / bucket_snapshot.REPOSITORY_DIRNAME
            repository.mkdir(parents=True)
            metadata = bucket_snapshot.desired_metadata("0" * 64, self.revisions)
            manifest = {
                "schema": 3,
                "index_metadata": metadata,
                "archive_commits": {str(key): value for key, value in self.revisions.items()},
                "snapshot": "bha-1-aaaaaaaa",
                "generation": "bha-1-aaaaaaaa",
                "physical_index": "article-build",
                "document_count": 1,
                "sidecars": {"docs": {"filename": "docs.sqlite"}, "facets": {"filename": "facets.sqlite"}},
            }
            bucket.mkdir(exist_ok=True)
            (bucket / "current.json").write_text(json.dumps(manifest), encoding="utf-8")
            client = MagicMock()
            client.indices.exists.return_value = False
            client.snapshot.restore.side_effect = RuntimeError("restore failed")
            with patch.object(bucket_snapshot, "Elasticsearch", return_value=client), \
                    patch.object(bucket_snapshot, "parsed_archive_commits", return_value=self.revisions), \
                    patch.object(bucket_snapshot, "local_repository", return_value=root / "local-repository"):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertFalse(bucket_snapshot.restore(bucket))
            deleted = [call.kwargs["index"] for call in client.indices.delete.call_args_list]
            self.assertEqual(deleted, ["article", "article-build"])
            self.assertIn("stage=restore_elasticsearch", output.getvalue())
            self.assertIn("detail=restore failed", output.getvalue())
            self.assertIn("restore_elasticsearch", bucket_snapshot.snapshot_status()["stage_seconds"])

    def test_sidecar_copy_retries_before_accepting_valid_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite"
            destination = root / "destination.sqlite"
            source.write_bytes(b"valid sidecar")
            metadata = bucket_snapshot.file_metadata(source)
            real_copy = bucket_snapshot.copy_and_hash
            attempts = 0

            def flaky_copy(source_path, destination_path):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    Path(destination_path).write_bytes(b"short")
                    return 5, "0" * 64
                return real_copy(source_path, destination_path)

            with patch.object(bucket_snapshot, "copy_and_hash", side_effect=flaky_copy), \
                    patch.object(bucket_snapshot.time, "sleep"):
                bucket_snapshot.copy_validated(source, destination, metadata)
            self.assertEqual(attempts, 2)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_invalid_recovery_limit_is_not_applied(self):
        client = MagicMock()
        with patch.object(bucket_snapshot, "RESTORE_RECOVERY_MAX_BYTES_PER_SEC", "invalid"):
            self.assertFalse(bucket_snapshot.configure_restore_recovery(client))
        client.cluster.put_settings.assert_not_called()

    def test_startup_uses_local_repository_and_reset_bypasses_restore(self):
        script = (Path(__file__).parents[1] / "scripts" / "start.sh").read_text(encoding="utf-8")
        self.assertIn("path.repo: ${snapshot_repo_root}", script)
        self.assertIn('[ "${RESET_INDEX:-0}" != "1" ]', script)
        self.assertIn('source_stale', script)
        self.assertIn('if [ ! -f "${convergence_plan}" ]', script)
        self.assertIn('waiting for an explicit revision update', script)
        self.assertNotIn('BHA_PARSED_CACHE_ROOT="${bucket_dir}/parsed-cache"', script)
        self.assertNotIn("path.repo: %s", script)
        self.assertIn('wait -n "${api_pid}" "${es_pid}" "${indexer_pid}"', script)


if __name__ == "__main__":
    unittest.main()
