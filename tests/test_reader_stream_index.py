import io
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from scripts import reader_stream_index as index
from scripts import pilot_epub_chapters as ebook
from scripts import pilot_media_stream as media
from scripts import pilot_spreadsheet_stream as spreadsheet
from scripts import pilot_web_stream as web


BUCKET = "vomebook/reader-assets-v2"


class ReaderStreamCheckpointTests(unittest.TestCase):
    def test_index_reads_fail_closed_except_confirmed_missing(self):
        fs = Mock()
        with patch.object(index, "HfFileSystem", return_value=fs):
            fs.open.side_effect = FileNotFoundError("missing")
            self.assertEqual(index.read_index(BUCKET, "index.json", None, "fixture")["files"], [])
            for error in (PermissionError("denied"), OSError("network")):
                fs.open.side_effect = error
                with self.assertRaises(type(error)):
                    index.read_index(BUCKET, "index.json", None, "fixture")
            fs.open.side_effect = None
            for raw in (b"[]", b"not json", b'{"files":{}}', b'{"files":[],"failures":{}}',
                        b'{"files":[{"path":"missing key"}]}'):
                fs.open.return_value = io.BytesIO(raw)
                with self.assertRaises(ValueError):
                    index.read_index(BUCKET, "index.json", None, "fixture")

    def test_failed_new_revision_keeps_good_old_mapping_and_failure(self):
        key = "repo\0book.epub"
        good = {"key": key, "source_revision": "old"}
        failure = {"key": key, "source_revision": "new", "error": "bad"}
        result = index.merge_indexes({"files": [good]}, [], [failure], "fixture")
        self.assertEqual(result["files"], [good])
        self.assertEqual(result["failures"], [failure])
        self.assertEqual(index.completed_revisions(result)[key], "new")
        self.assertEqual(index.completed_revisions(result, True)[key], "old")
        recovered = {"key": key, "source_revision": "new"}
        self.assertEqual(index.merge_indexes(result, [recovered], [], "fixture")["failures"], [])
        recovered = {**recovered, "source_revision": "newer", "checkpointed_at": "2026-10-22T15:00:00+00:00"}
        self.assertEqual(index.merge_indexes({"files": [recovered]}, [], [failure], "fixture")["failures"], [])

    def test_new_chm_profile_failure_is_not_hidden_by_old_same_revision_success(self):
        good = {"key": "repo\0book.chm", "source_revision": "same", "profile": "old"}
        failure = {**good, "profile": "new", "error": "bad"}
        result = index.merge_indexes({"files": [good]}, [], [failure], "fixture")
        self.assertEqual(result["failures"], [failure])

    def test_canonical_merge_preserves_office_and_ignores_other_shards_stale_copies(self):
        office = {"key": "repo\0book.docx", "source_revision": "new-office", "source_extension": "docx"}
        chapter = {"key": "repo\0book.epub", "source_revision": "old", "source_extension": "epub"}
        shard = index.shard_for(chapter["key"], 8)
        canonical = {"files": [office, chapter]}
        new = {**chapter, "source_revision": "new", "checkpointed_at": "2026-10-22T12:00:00+00:00"}
        stale = {"files": [{**office, "source_revision": "old-office"}, chapter]}
        result = index.merge_shards(canonical, [(shard, {"files": [new]}), ((shard + 1) % 8, stale)],
                                    "ebook-chapter-stream-index", {"epub"})
        self.assertEqual({e["key"]: e for e in result["files"]}, {office["key"]: office, chapter["key"]: new})
        later_canonical = {"files": [{**new, "checkpointed_at": "2026-10-22T13:00:00+00:00"}]}
        self.assertEqual(index.merge_shards(later_canonical, [(shard, {"files": [new]})],
                                           "fixture", {"epub"})["files"], later_canonical["files"])

    def test_content_is_protected_and_uploaded_before_checkpoint(self):
        events = []
        entry = {"key": "repo\0book.html", "source_revision": "rev", "object": "documents/web/html/hash/document.html"}
        def upload(bucket, files, token):
            events.append([(remote, Path(local).read_bytes()) for local, remote in files])
        with tempfile.TemporaryDirectory() as temporary, patch.object(index, "batch_bucket_files_with_retry", side_effect=upload), \
                patch.object(index, "remote_identity", side_effect=[None, (hashlib.sha256(b'body').hexdigest(), 4)]), \
                patch.object(index, "read_index", side_effect=lambda *args: json.loads(events[-1][0][1])), \
                patch.object(index, "HfFileSystem"):
            work = Path(temporary)
            content = work / "document.html"
            content.write_bytes(b"body")
            result = index.publish_checkpoint(BUCKET, "documents/web/html/index.json", {"files": []},
                                               [entry], [], {entry["object"]: str(content)}, work,
                                               "token", apply=True, kind="fixture")
        self.assertEqual(len(events), 3)
        self.assertTrue(events[0][0][0].startswith("reader-index/processing/stream-upload-"))
        self.assertEqual(events[1], [(entry["object"], b"body")])
        self.assertEqual(events[2][0][0], "documents/web/html/index.json")
        self.assertTrue(result["files"][0]["processing_roots"])

    def test_failure_only_batch_persists_before_strict_exit_and_limit_counts_attempts(self):
        events = []
        source = {"repo": "repo", "revision": "rev", "extension": "html"}
        def upload(_bucket, files, _token):
            events.extend(json.loads(Path(local).read_text()) for local, remote in files if remote.endswith("index.json"))
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(index, "read_index", side_effect=lambda *args: events[-1] if events else {"files": []}), \
                patch.object(index, "batch_bucket_files_with_retry", side_effect=upload):
            build = Mock(side_effect=RuntimeError("HTTP 429"))
            code = index.run_documents([{**source, "path": f"{i}.html"} for i in range(3)], bucket=BUCKET,
                token=None, limit=2, retry_failures=False, strict=True, apply=True, prefix="documents/web",
                kind="fixture", build=build, work=Path(temporary), checkpoint_every=1)
        self.assertEqual(code, 1)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(events[-1]["failures"]), 2)
        self.assertIn("HTTP 429", events[-1]["failures"][0]["error"])
        self.assertTrue(events[-1]["failures"][0]["checkpointed_at"])

    def test_corrupt_content_readback_never_advances_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            content = work / "document.html"
            content.write_bytes(b"body")
            entry = {"key": "repo\0book.html", "source_revision": "rev", "object": "documents/web/html/hash/document.html"}
            with patch.object(index, "batch_bucket_files_with_retry") as upload, \
                    patch.object(index, "remote_identity", side_effect=[None, ("wrong", 1)]), \
                    patch.object(index, "HfFileSystem"):
                with self.assertRaisesRegex(ValueError, "readback failed"):
                    index.publish_checkpoint(BUCKET, "documents/web/html/index.json", {"files": []},
                        [entry], [], {entry["object"]: str(content)}, work, None, apply=True, kind="fixture")
            self.assertEqual(upload.call_count, 2)
            self.assertFalse(any(remote.endswith("index.json") for call in upload.call_args_list
                                 for _local, remote in call.args[1]))

    def test_s3_readback_streams_sha_and_missing_is_not_an_access_error(self):
        from botocore.exceptions import ClientError
        client = Mock()
        client.get_object.return_value = {"Body": io.BytesIO(b"complete bytes")}
        with patch.dict("os.environ", {"HF_S3_ACCESS_KEY_ID": "fixture", "HF_S3_SECRET_ACCESS_KEY": "fixture"}), \
                patch.object(index, "s3_client", return_value=client):
            self.assertEqual(index.remote_identity(Mock(), BUCKET, "documents/x"),
                             (hashlib.sha256(b"complete bytes").hexdigest(), 14))
            for code, expected in (("NoSuchKey", None), ("AccessDenied", PermissionError)):
                client.get_object.side_effect = ClientError({"Error": {"Code": code}}, "GetObject")
                if expected is None:
                    self.assertIsNone(index.remote_identity(Mock(), BUCKET, "documents/x"))
                else:
                    with self.assertRaises(ClientError):
                        index.remote_identity(Mock(), BUCKET, "documents/x")

    def test_source_sha_reuses_only_verified_lfs_identity_at_pinned_revision(self):
        rows = [{"key": "repo\0same.mp3", "revision": "new"}, {"key": "repo\0changed.mp3", "revision": "new"}]
        previous = {"files": [{"key": row["key"], "source_revision": "old", "source_sha256": "a" * 64} for row in rows]}
        api = Mock()
        api.get_paths_info.return_value = [SimpleNamespace(path="same.mp3", lfs=SimpleNamespace(sha256="a" * 64)),
                                           SimpleNamespace(path="changed.mp3", lfs={"sha256": "b" * 64})]
        with patch.object(index, "HfApi", return_value=api):
            self.assertEqual(index.unchanged_sources(rows, previous, "token"), {"repo\0same.mp3"})
        api.get_paths_info.assert_called_once_with("repo", ["same.mp3", "changed.mp3"], revision="new", repo_type="dataset")

    def test_mislabeled_transcoded_checkpoint_is_requeued_even_with_same_source_sha(self):
        bad = {"key": "repo\0book.wav", "source_revision": "same", "source_sha256": "a" * 64,
               "profile": "ffmpeg-audio-mp3-v1", "object": "media/audio/wav/hash/audio.wav"}
        self.assertNotIn(bad["key"], index.completed_revisions({"files": [bad]}))
        with patch.object(index, "HfApi") as api:
            self.assertEqual(index.unchanged_sources([{"key": bad["key"], "revision": "new"}], {"files": [bad]}, None), set())
        api.return_value.get_paths_info.assert_not_called()


class FormatProducerRegressionTests(unittest.TestCase):
    def test_shared_source_download_honors_rate_limit_countdown(self):
        import urllib.error
        from scripts import convert_reader_assets as converter
        error = urllib.error.HTTPError("https://example.test/source", 429, "limited", {"Retry-After": "27"}, io.BytesIO(b"limited"))
        response = Mock(status=200, headers={})
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        response.read.side_effect = [b"complete", b""]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(converter.urllib.request, "urlopen", side_effect=[error, response]), \
                patch.object(converter.time, "sleep") as sleep:
            digest, size = converter.download_source("https://example.test/source", Path(temporary) / "source")
        sleep.assert_called_once_with(27)
        self.assertEqual(size, 8)
        self.assertEqual(digest, hashlib.sha256(b"complete").hexdigest())

    def test_media_publishes_actual_transcoded_filename_kind_and_digest(self):
        cases = [("m4a", "audio", "audio.mp3"), ("mov", "video", "video.mp4"), ("asx", "audio", "audio.mp3")]
        for extension, mode, filename in cases:
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                def convert(item, bundle):
                    target = bundle / "objects/hash" / filename
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b"converted")
                    return {"path": target.relative_to(bundle).as_posix(), "reader_mode": mode,
                            "source_sha256": "a" * 64, "sha256": "b" * 64, "profile": "transcoded"}
                with patch.object(media, "convert_item", side_effect=convert):
                    entry, uploads = media.build_one({"repo": "repo", "path": "book." + extension,
                        "extension": extension, "revision": "rev", "source_bytes": 100}, work, BUCKET)
                self.assertEqual(entry["mode"], mode)
                self.assertEqual(entry["object"], f"media/{mode}/{extension}/{'b' * 64}/{filename}")
                self.assertEqual(Path(uploads[entry["object"]]).read_bytes(), b"converted")

    def test_csv_and_ods_do_not_use_office_encryption_parser(self):
        for extension in ("csv", "ods"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                def download(_url, target, _token): target.write_bytes(b"source")
                def convert(_source, target, _work, _item): target.write_text("<p>complete</p>")
                with patch.object(spreadsheet, "download", side_effect=download), \
                        patch("scripts.convert_reader_assets.convert_spreadsheet_to_html", side_effect=convert), \
                        patch("msoffcrypto.OfficeFile", side_effect=AssertionError("not an Office container")):
                    entry, uploads = spreadsheet.build({"repo": "repo", "path": "book." + extension,
                        "extension": extension, "revision": "rev"}, work / "book", None, BUCKET)
                self.assertIn(entry["object"], uploads)

    def test_web_asset_identity_changes_with_inlined_resources(self):
        paths = []
        with tempfile.TemporaryDirectory() as temporary:
            for number, output in enumerate(("<p>same text</p>", "<p>same text, updated resource</p>")):
                work = Path(temporary) / str(number)
                def download(_url, target, _token): target.write_bytes(b"same-source")
                def inline(_source, _url, root):
                    result = root / "document.html"
                    result.write_text(output)
                    return result
                with patch.object(web, "download", side_effect=download), \
                        patch.object(web, "inline_html_resources", side_effect=inline):
                    entry, _ = web.build_one({"repo": "repo", "path": "book.html", "extension": "html", "revision": "rev"}, work, None, BUCKET)
                    paths.append(entry["object"])
        self.assertNotEqual(*paths)

    def test_ebook_strict_failure_checkpoints_before_returning_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            revisions = root / "revisions.json"
            revisions.write_text('{}')
            args = SimpleNamespace(limit=0, max_source_bytes=0, workers=1, shard_count=1, shard_index=0,
                extension="epub", bucket=BUCKET, retry_failures=False, failed_only=False, strict=True,
                apply=True, plan_only=False, revisions=revisions, search_data=root / "search.json")
            with patch.object(ebook, "parse_args", return_value=args), \
                    patch.object(ebook, "existing_index", return_value={"files": []}), \
                    patch.object(ebook, "select_records", return_value=[{"repo": "repo", "path": "bad.epub", "revision": "rev"}]), \
                    patch.object(ebook, "unchanged_sources", return_value=set()), \
                    patch.object(ebook, "build_one", side_effect=RuntimeError("broken EPUB")), \
                    patch.object(ebook, "publish_checkpoint", return_value={"files": []}) as publish:
                self.assertEqual(ebook.main(), 1)
            failure = publish.call_args.args[4][0]
            self.assertEqual(failure["source_extension"], "epub")
            self.assertIn("broken EPUB", failure["error"])

    def test_restored_workflows_are_strict_retryable_and_reconcile_empty_matrices(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        for name, job in (("ebook", "ebook-stream"), ("web", "web-stream"),
                          ("spreadsheet", "spreadsheet-stream"), ("media", "media")):
            with self.subTest(format=name):
                workflow = yaml.safe_load((root / f"reader-{name}-stream.yml").read_text())
                commands = "\n".join(s.get("run", "") for s in workflow["jobs"][job]["steps"])
                self.assertIn("--apply --strict", commands)
                self.assertIn("--retry-failures", commands)
                self.assertIn('== "schedule" ||', commands)
                self.assertIn(f"needs.{job}.result == 'success'", workflow["jobs"]["finalize"]["if"])
                self.assertIn(f"needs.{job}.result == 'skipped'", workflow["jobs"]["finalize"]["if"])
                self.assertNotIn("!= 'cancelled'", workflow["jobs"]["finalize"]["if"])
                self.assertEqual(workflow["jobs"]["finalize"]["concurrency"]["group"], "reader-sidecar")


if __name__ == "__main__":
    unittest.main()
