import copy
import gzip
import hashlib
import json
import subprocess
import tempfile
import sys
import unittest
import httpx
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import pdf_ocr_stages as stages, publish_pdf_ocr_assets as publication
from scripts import publish_search_reader_index as pages, reader_bucket, reader_lifecycle, shared


class PdfPublicationRecoveryTests(unittest.TestCase):
    def test_ordered_reads_bound_admission_and_preserve_all_items(self):
        admitted = []
        def items():
            for number in range(20):
                admitted.append(number)
                yield number
        reads = shared.ordered_reads(lambda value: value, items(), 4)
        self.assertEqual(next(reads), 0)
        self.assertEqual(admitted, list(range(4)))
        self.assertEqual([0, *reads], list(range(20)))
        self.assertEqual(admitted, list(range(20)))
        for workers in (0, 9):
            with self.assertRaises(ValueError):
                list(shared.ordered_reads(lambda value: value, [1], workers))

    def test_bad_object_is_retried_but_never_accepted(self):
        data = b"verified PNG"
        meta = {"path": "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr-input/page-000001.png",
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        session = Mock()
        session.get.side_effect = [Mock(content=b"short"), Mock(content=data)]
        with patch.object(stages, "get_session", return_value=session), \
                patch.object(stages, "hf_raise_for_status"), patch.object(stages.time, "sleep"):
            self.assertEqual(stages.read_object(meta), data)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs["headers"], {"Cache-Control": "no-cache"})
        session.get.side_effect = None
        session.get.return_value = Mock(content=b"corrupt")
        session.get.reset_mock()
        with patch.object(stages, "get_session", return_value=session), \
                patch.object(stages, "hf_raise_for_status"), patch.object(stages.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                stages.read_object(meta)
        self.assertEqual(session.get.call_count, 3)

    def test_bucket_transport_retry_preserves_complete_reads_and_never_retries_missing(self):
        session = Mock()
        session.get.side_effect = [httpx.RemoteProtocolError("partial transport"), Mock(status_code=200, content=b"complete")]
        with patch.object(reader_bucket, "get_session", return_value=session), \
                patch.object(reader_bucket, "hf_raise_for_status"), patch.object(reader_bucket.time, "sleep"):
            self.assertEqual(reader_bucket.read_bytes("reader-index/metadata.json"), b"complete")
        self.assertEqual(session.get.call_count, 2)
        session.get.reset_mock()
        session.get.side_effect = None
        session.get.return_value = Mock(status_code=404)
        with patch.object(reader_bucket, "get_session", return_value=session):
            with self.assertRaises(FileNotFoundError):
                reader_bucket.read_bytes("reader-index/missing.json")
        self.assertEqual(session.get.call_count, 1)

    def test_batch_upload_retries_the_same_records_without_changing_identity(self):
        observed = []
        def upload(bucket, add, token):
            observed.append({remote: Path(local).read_bytes() for local, remote in add})
            if len(observed) == 1:
                raise httpx.ReadTimeout("lost upload response")
        with patch.object(reader_bucket, "batch_bucket_files", side_effect=upload), \
                patch.object(reader_bucket.time, "sleep"):
            reader_bucket.publish_json_batch({"reader-index/processing/one.json": {"status": "processing"},
                                              "reader-index/processing/two.json": {"status": "processing"}})
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0], observed[1])

    def test_upload_batches_protection_before_all_books_and_skips_empty_input_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            for number in range(4):
                root = bundle / "objects" / "aa" / ("a" * 64) / f"{number:016x}"
                root.mkdir(parents=True)
                (root / "ocr-manifest.json").write_text("{}")
            events = []
            def protect(records, token):
                events.append(("protect", copy.deepcopy(records)))
            with patch.object(stages, "publish_json_batch", side_effect=protect), \
                    patch.object(stages, "remote_sizes", return_value={}), \
                    patch.object(stages.shared, "batch_bucket_files_with_retry",
                                 side_effect=lambda bucket, additions, token: events.append(("upload", bucket, additions))):
                paths = stages.upload_objects(bundle)
            self.assertEqual([event[0] for event in events], ["protect", *["upload"] * 4, "protect"])
            self.assertEqual(len(paths), 4)
            self.assertTrue(all(value["status"] == "processing" for value in events[0][1].values()))
            self.assertTrue(all(value["status"] == "uploaded" for value in events[-1][1].values()))
            uploads = [(event[1], event[2]) for event in events if event[0] == "upload"]
            self.assertTrue(all(bucket == shared.PDF_PAGES_BUCKET and additions for bucket, additions in uploads))

    def test_handoff_is_batched_idempotent_and_validates_all_owners_first(self):
        paths = [f"reader-index/processing/{number}.json" for number in range(4)]
        records = {path: reader_lifecycle.processing_record([
            {"bucket": shared.PDF_PAGES_BUCKET, "root": f"objects/aa/book/{number}"}], "test", {})
                   for number, path in enumerate(paths)}
        results = [{"ocr_manifest": f"objects/aa/book/{number}/ocr-manifest.json", "processing_roots": [path]}
                   for number, path in enumerate(paths)]
        with patch.object(publication, "read_bucket_json", side_effect=lambda path, token: records[path]), \
                patch.object(publication, "publish_json_batch") as write:
            publication.release_processing_roots(results, "generation", None)
            self.assertEqual(write.call_count, 1)
            released = write.call_args.args[0]
            self.assertEqual(set(released), set(paths))
            records.update(released)
            write.reset_mock()
            publication.release_processing_roots(results, "generation", None)
            write.assert_not_called()
            records[paths[-1]] = reader_lifecycle.processing_record([
                {"bucket": shared.PDF_PAGES_BUCKET, "root": "objects/foreign/book/profile"}], "test", {})
            with self.assertRaises(ValueError):
                publication.release_processing_roots(results, "generation", None)
            write.assert_not_called()

    def test_publication_checkpoints_whole_book_batches_and_preserves_prior_success(self):
        books = [{"key": f"book-{number}", "status": "ready"} for number in range(7)]
        committed = []
        def assemble(book, saved, bundle):
            root = f"objects/aa/{'a' * 64}/{int(book['key'].split('-')[1]):016x}"
            out = bundle / root / "ocr-manifest.json"
            out.parent.mkdir(parents=True)
            out.write_text("{}")
            return {**book, "ocr_manifest": root + "/ocr-manifest.json", "page_count": 999}
        def upload(bundle):
            return [f"reader-index/processing/{root.name}.json" for root in sorted((bundle / "objects").glob("*/*/*"))]
        def publish(api, repo, results):
            if committed:
                raise RuntimeError("interrupted second batch")
            committed.extend(copy.deepcopy(results))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stages, "assemble_book", side_effect=assemble), \
                patch.object(stages, "upload_objects", side_effect=upload), \
                patch.object(publication, "publish", side_effect=publish):
            with self.assertRaisesRegex(RuntimeError, "second batch"):
                stages.publish_ocr_batches(books, {}, Mock(), "repo", Path(directory))
            checkpoint = json.loads((Path(directory) / "publication.json").read_text())
        self.assertEqual([r["key"] for r in committed], [r["key"] for r in books[:4]])
        self.assertTrue(all(r["page_count"] == 999 and len(r["processing_roots"]) == 1 for r in committed))
        self.assertEqual(checkpoint["selected_books"], 7)
        self.assertFalse(checkpoint["complete"])
        self.assertEqual(checkpoint["batches"][0]["ready"], 4)

    def test_complete_batches_include_the_tail_without_truncation(self):
        books = [{"key": f"book-{number}", "status": "skipped"} for number in range(7)]
        with tempfile.TemporaryDirectory() as directory, patch.object(publication, "publish") as publish:
            report = stages.publish_ocr_batches(books, {}, Mock(), "repo", Path(directory), 4)
        self.assertTrue(report["complete"])
        self.assertEqual([len(call.args[2]) for call in publish.call_args_list], [4, 3])
        self.assertEqual(sum(b["books"] for b in report["batches"]), 7)

    def test_unchanged_catalog_verifies_existing_snapshot_without_reupload(self):
        sidecar = {"v": 1, "f": {}}
        catalog, generation = reader_lifecycle.catalog_generation(sidecar)
        compressed = gzip.compress(json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode(), mtime=0)
        catalog["generations"][generation]["snapshot"] = {
            "bucket": shared.READER_ASSETS_BUCKET,
            "path": f"reader-index/generations/{generation}/reader_assets.json.gz",
            "sha256": hashlib.sha256(compressed).hexdigest(), "bytes": len(compressed)}
        with patch.object(reader_bucket, "read_json", return_value=catalog), \
                patch.object(reader_bucket, "read_bytes", return_value=compressed), \
                patch.object(reader_bucket, "publish_indexes") as upload, patch.object(reader_bucket, "publish_json") as write:
            self.assertEqual(reader_bucket.publish_catalog(sidecar), generation)
        upload.assert_not_called()
        write.assert_not_called()

    def test_catalog_metadata_repair_does_not_mutate_or_mistake_prior_pointer_for_current(self):
        sidecar = {"v": 1, "f": {}}
        catalog, generation = reader_lifecycle.catalog_generation(sidecar)
        prior = copy.deepcopy(catalog)
        with patch.object(reader_bucket, "read_json", return_value=catalog), \
                patch.object(reader_bucket, "read_bytes") as read, \
                patch.object(reader_bucket, "publish_indexes") as upload, patch.object(reader_bucket, "publish_json") as write:
            self.assertEqual(reader_bucket.publish_catalog(sidecar), generation)
        self.assertEqual(catalog, prior)
        read.assert_not_called()
        upload.assert_called_once()
        write.assert_called_once()

    def test_superseded_queue_never_downgrades_new_native_page_owner(self):
        book = {"key": "book", "status": "ready", "source_kind": "upstream", "source_revision": "old",
                "source_sha256": "a" * 64, "render_manifest": {"sha256": "b" * 64}, "page_count": 300}
        queue = {"books": [book], "shards": [[{"key": "book", "pages": [187]}]]}
        self.assertEqual(stages.superseded_ocr_books(queue, {"book": book}), set())
        new = {**book, "source_revision": "new", "render_manifest": {"sha256": "c" * 64}}
        excluded = stages.superseded_ocr_books(queue, {"book": new})
        filtered = stages.filter_ocr_queue(queue, excluded)
        self.assertEqual(filtered["books"], [])
        self.assertEqual(filtered["shards"], [])
        self.assertEqual(queue["shards"][0][0]["pages"], [187])
        with self.assertRaisesRegex(ValueError, "current render owner"):
            stages.superseded_ocr_books(queue, {})
        dictionary_queue = {**queue, "shards": [{"index": 0, "records": [{"key": "book"}]}]}
        self.assertEqual(stages.filter_ocr_queue(dictionary_queue, excluded)["shards"], [])

    def test_old_queue_cli_retains_protection_without_publishing_failed_state(self):
        book = {"key": "book", "status": "ready", "source_kind": "upstream", "source_revision": "old",
                "source_sha256": "a" * 64, "render_manifest": {"sha256": "b" * 64}, "page_count": 300}
        latest = {**book, "source_revision": "new", "render_manifest": {"sha256": "c" * 64}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.json"
            queue.write_text(json.dumps({"version": 1, "kind": "pdf-image-ocr-queue", "books": [book], "shards": []}))
            output = root / "output"
            with patch.object(sys, "argv", ["pdf_ocr_stages.py", "publish-ocr", "--queue", str(queue), "--output", str(output)]), \
                    patch.object(stages, "load_registry", return_value={"files": {"book": latest}}), \
                    patch.object(stages, "save_registry") as save, \
                    patch.object(stages, "handoff_worker_results") as handoff, \
                    patch.object(publication, "publish") as publish:
                self.assertEqual(stages.main(), 0)
            report = json.loads((output / "publication.json").read_text())
        self.assertEqual(report["superseded_books"], 1)
        save.assert_not_called()
        publish.assert_not_called()
        handoff.assert_not_called()

    def test_pages_retry_reclones_and_never_force_pushes(self):
        commands, pushes = [], 0
        def run(args, cwd=None, env=None):
            nonlocal pushes
            commands.append((args, cwd))
            if args[:2] == ["git", "clone"]:
                (Path(args[-1]) / "data").mkdir()
            if args[:3] == ["git", "status", "--porcelain"]:
                return 0, " M data/reader_assets.json.gz", ""
            if args == ["git", "push"]:
                pushes += 1
                if pushes == 1:
                    return 1, "", "remote: fatal error in commit_refs"
            return 0, "", ""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sidecar.gz"
            source.write_bytes(b"coherent generation")
            with patch.object(pages, "run", side_effect=run), patch.object(pages.time, "sleep"):
                pages.publish_to_pages(source, "owner/repo", "token", dry_run=False)
        clones = [args[-1] for args, _ in commands if args[:2] == ["git", "clone"]]
        self.assertEqual(len(set(clones)), 2)
        self.assertEqual(pushes, 2)
        self.assertFalse(any("--force" in args or "reset" in args for args, _ in commands))

    def test_pages_does_not_retry_auth_failures_and_git_calls_are_bounded(self):
        with patch.object(pages, "_publish_to_pages_once", side_effect=RuntimeError("Pages push failed: authentication failed")) as run, \
                patch.object(pages.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                pages.publish_to_pages(Path("ignored"), "owner/repo", "token", dry_run=False)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        with patch.object(pages.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 180)):
            self.assertEqual(pages.run(["git", "push"])[0], 124)
