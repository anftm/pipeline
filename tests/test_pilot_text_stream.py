import codecs
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import urllib.request
from unittest.mock import patch

from scripts import pilot_text_stream as stream
from scripts.text_encoding import PROFILE, TextEncodingError, normalize_utf8


class TextNormalizationTests(unittest.TestCase):
    def test_unicode_boms_and_unmarked_utf16_preserve_complete_text(self):
        text = "中文🌼 literal\ufffd\r\nnext\tline\n"
        for codec in ("utf-8", "utf-8-sig", "utf-16", "utf-16-be", "utf-32"):
            with self.subTest(codec=codec):
                raw = text.encode(codec)
                if codec == "utf-16-be":
                    raw = codecs.BOM_UTF16_BE + raw
                output, metadata = normalize_utf8(raw)
                self.assertTrue(output.startswith(codecs.BOM_UTF8))
                self.assertEqual(output.decode("utf-8-sig"), text)
                self.assertEqual(metadata["characters"], len(text))
        for codec in ("utf-16-le", "utf-16-be"):
            text = "ASCII opening complete text 中文\r\n"
            output, metadata = normalize_utf8(text.encode(codec))
            self.assertEqual(output.decode("utf-8-sig"), text)
            self.assertEqual(metadata["source_encoding"], codec)

    def test_strict_legacy_scores_and_explicit_ambiguity_override(self):
        text = "中文文本和完整正文。"
        for codec in ("gb18030", "big5"):
            output, metadata = normalize_utf8(text.encode(codec))
            self.assertEqual(output.decode("utf-8-sig"), text)
            self.assertEqual(metadata["source_encoding"], codec)
        russian = "Русский текст"
        output, metadata = normalize_utf8(russian.encode("cp1251"), hint="Русский.txt")
        self.assertEqual(output.decode("utf-8-sig"), russian)
        self.assertEqual(metadata["source_encoding"], "cp1251")
        with self.assertRaises(TextEncodingError):
            normalize_utf8(b"\xa1\xa1")
        output, metadata = normalize_utf8(b"\xa1\xa1", encoding="big5")
        self.assertEqual(output.decode("utf-8-sig"), b"\xa1\xa1".decode("big5"))
        self.assertEqual(metadata["encoding_decision"], "explicit")

    def test_invalid_unicode_binary_and_lfs_are_not_silently_replaced(self):
        for raw in (codecs.BOM_UTF8 + b"\xff", codecs.BOM_UTF16_LE + b"x",
                    b"binary\0\x01payload", b"\xff",
                    b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n"):
            with self.subTest(raw=raw), self.assertRaises((UnicodeError, TextEncodingError)):
                normalize_utf8(raw)
        self.assertEqual(normalize_utf8(b"")[0], codecs.BOM_UTF8)
        with self.assertRaises(TextEncodingError):
            normalize_utf8(b"text", encoding="not-a-codec")

    def test_explicit_utf8_normalization_is_bom_idempotent(self):
        raw = codecs.BOM_UTF8 + "text\ufffd\r\n".encode()
        self.assertEqual(normalize_utf8(raw, encoding="utf-8")[0], raw)


class TextStreamPublicationTests(unittest.TestCase):
    def test_download_strips_authorization_on_signed_host_redirects_and_keeps_size_bound(self):
        class Response(io.BytesIO):
            headers = {"Content-Length": "6"}
        def opener(handler):
            original = urllib.request.Request("https://huggingface.co/source", headers={"Authorization": "Bearer test"})
            redirected = handler.redirect_request(original, None, 302, "Found", {}, "https://cdn.invalid/file")
            self.assertFalse(redirected.has_header("Authorization"))
            same = handler.redirect_request(original, None, 302, "Found", {}, "https://huggingface.co/target")
            self.assertEqual(same.get_header("Authorization"), "Bearer test")
            with self.assertRaises(ValueError):
                handler.redirect_request(original, None, 302, "Found", {}, "http://cdn.invalid/file")
            return SimpleNamespace(open=lambda *args, **kwargs: Response(b"abcdef"))
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(stream.urllib.request, "build_opener", side_effect=opener), \
             patch.object(stream, "MAX_SOURCE_BYTES", 5):
            with self.assertRaises(ValueError):
                stream.download("https://huggingface.co/source", Path(directory) / "out", "test")

    def test_plan_never_downloads_and_byte_checkpoints_do_not_skip_later_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps([{"Repo": "VoiceOfML/Test", "File": name, "Extension": "txt", "Folder": []}
                                          for name in ("one", "two", "three", "utf8")]))
            revisions = root / "revisions.json"
            revisions.write_text(json.dumps({"VoiceOfML/Test": "a" * 40}))
            args = SimpleNamespace(search_data=source, revisions=revisions, extensions=["txt"], limit=0,
                checkpoint_size=64, bucket=stream.TARGET_BUCKET, apply=False, plan_only=True,
                force=False, retry_failures=False, encoding_overrides=None, report=root / "report.json")
            previous = {"version": 1, "kind": "text-stream-index", "files": []}
            def download(url, target, token):
                target.write_bytes(b"\xff" if url.endswith("two.txt") else b"Complete text\r\n"
                                   if url.endswith("utf8.txt") else "中文文本和完整正文。\r\n".encode("gb18030"))
            with patch.object(stream, "parse_args", return_value=args), \
                 patch.object(stream, "existing_index", return_value=previous), \
                 patch.object(stream, "download", side_effect=download) as fetch, \
                 patch.object(stream, "batch_bucket_files_with_retry") as upload, \
                 patch.object(stream, "CHECKPOINT_BYTES", 1), \
                 patch.dict(os.environ, {"GITHUB_OUTPUT": str(root / "actions-output")}):
                self.assertEqual(stream.main(), 0)
                fetch.assert_not_called()
                self.assertEqual(json.loads(args.report.read_text())["selected"], 4)
                args.plan_only = False
                self.assertEqual(stream.main(), 1)
                report = json.loads(args.report.read_text())
                self.assertEqual(report["attempted"], 4)
                self.assertEqual(report["converted"], 2)
                self.assertEqual(len(report["unchanged_utf8"]), 1)
                self.assertEqual(report["published"], 0)
                self.assertEqual(len(report["failures"]), 1)
                self.assertTrue(report["checkpoint_complete"])
                self.assertEqual(fetch.call_count, 4)
                upload.assert_not_called()

    def test_utf8_sources_are_unchanged_and_selection_checksum_is_checked_first(self):
        item = {"repo":"VoiceOfML/Test", "path":"plain.md", "extension":"md", "revision":"a" * 40}
        raw = b"Complete **UTF-8** text.\r\n"
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(stream, "download", side_effect=lambda url, target, token:target.write_bytes(raw)):
            root = Path(directory)
            with self.assertRaises(stream.Utf8SourceUnchanged) as caught:
                stream.build_one(item, root / "unchanged", None, stream.TARGET_BUCKET, skip_utf8=True)
            self.assertEqual(caught.exception.metadata["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertFalse((root / "unchanged/document.md").exists())
            with self.assertRaisesRegex(ValueError, "approved selection"):
                stream.build_one(item, root / "mismatch", None, stream.TARGET_BUCKET,
                                 skip_utf8=True, expected_sha256="0" * 64)

    def test_utf8_only_apply_does_not_republish_index_or_create_protection_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/"source.json";source.write_text(json.dumps([
                {"Repo":"VoiceOfML/Test","File":"readme","Extension":"md","Folder":[]}]))
            revisions=root/"revisions.json";revisions.write_text(json.dumps({"VoiceOfML/Test":"a"*40}))
            args=SimpleNamespace(search_data=source,revisions=revisions,extensions=["md"],limit=0,
                checkpoint_size=64,bucket=stream.TARGET_BUCKET,apply=True,plan_only=False,
                force=False,retry_failures=False,encoding_overrides=None,report=root/"report.json")
            with patch.object(stream,"parse_args",return_value=args), \
                 patch.object(stream,"existing_index",return_value={"version":1,"kind":"text-stream-index","files":[]}), \
                 patch.object(stream,"download",side_effect=lambda url,target,token:target.write_bytes(b"# Existing UTF-8\n")), \
                 patch.object(stream,"publish_checkpoint") as publish, \
                 patch.dict(os.environ,{"HF_TOKEN":"test","GITHUB_OUTPUT":str(root/"outputs")}):
                self.assertEqual(stream.main(),0)
                publish.assert_not_called()
                report=json.loads(args.report.read_text())
                self.assertEqual(report["published"],0)
                self.assertEqual(len(report["unchanged_utf8"]),1)

    def test_shared_content_has_one_deterministic_manifest_not_source_dependent_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = []
            text = "中文文本和完整正文。\r\n"
            for index, codec in enumerate(("utf-8", "gb18030")):
                raw = text.encode(codec)
                item = {"repo": "VoiceOfML/Test", "path": f"book{index}.md", "extension": "md", "revision": "a" * 40}
                with patch.object(stream, "download", side_effect=lambda url, target, token: target.write_bytes(raw)):
                    entry, files = stream.build_one(item, root / str(index), None, stream.TARGET_BUCKET)
                self.assertEqual(entry["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(entry["profile"], PROFILE)
                results.append((entry, {path: Path(local).read_bytes() for path, local in files.items()}))
            self.assertEqual(results[0][0]["object"], results[1][0]["object"])
            self.assertEqual(results[0][1], results[1][1])

    def test_old_profile_and_failure_checkpoints_are_not_claimed_verified(self):
        records = [{"repo": "VoiceOfML/Test", "path": f"{i}.txt", "revision": "a" * 40} for i in range(3)]
        previous = {"files": [{"key": "VoiceOfML/Test\0" + records[0]["path"], "source_revision": "a" * 40},
                              {"key": "VoiceOfML/Test\0" + records[1]["path"], "source_revision": "a" * 40, "profile": PROFILE}],
                    "failures": [{"key": "VoiceOfML/Test\0" + records[2]["path"], "source_revision": "a" * 40, "profile": PROFILE}]}
        self.assertEqual(stream.pending_records(records, previous), [records[0]])
        self.assertEqual(stream.pending_records(records, previous, retry_failures=True), [records[0], records[2]])
        self.assertEqual(stream.pending_records(records, previous, force=True), records)

    def test_index_reads_fail_closed_except_a_genuinely_missing_index(self):
        with patch.object(stream, "read_json", side_effect=FileNotFoundError):
            self.assertEqual(stream.existing_index(stream.TARGET_BUCKET, None)["files"], [])
        for error in (PermissionError, OSError, ValueError):
            with patch.object(stream, "read_json", side_effect=error), self.assertRaises(error):
                stream.existing_index(stream.TARGET_BUCKET, None)
        with patch.object(stream, "read_json", return_value={"version": 1, "kind": "text-stream-index", "files": [None]}):
            with self.assertRaises(ValueError):
                stream.existing_index(stream.TARGET_BUCKET, None)

    def publication(self, corrupt=False, racing=False):
        root = Path(self.directory)
        previous = {"version": 1, "kind": "text-stream-index", "files": [{"key": "old", "object": "old.txt"}]}
        expected = copy.deepcopy(previous)
        values, calls = {}, []
        def upload(bucket, additions, token):
            nonlocal expected
            for local, remote in additions:
                values[remote] = Path(local).read_bytes()
                calls.append(remote)
                if remote == stream.ROOT + "/index.json":
                    expected = json.loads(values[remote])
        def read(remote, token, bucket):
            if remote not in values:
                raise FileNotFoundError(remote)
            return b"corrupt" if corrupt else values[remote]
        def index(bucket, token):
            return {**expected, "race": True} if racing else expected
        local = root / "document.txt"
        local.write_bytes(codecs.BOM_UTF8 + b"complete needle")
        object_path = "documents/text/txt/" + hashlib.sha256(local.read_bytes()).hexdigest() + "/document.txt"
        entries = [{"key": "new", "object": object_path, "source_revision": "a" * 40, "profile": PROFILE}]
        with patch.object(stream, "batch_bucket_files_with_retry", side_effect=upload), \
             patch.object(stream, "read_bytes", side_effect=read), \
             patch.object(stream, "existing_index", side_effect=index):
            if corrupt or racing:
                with self.assertRaises(ValueError):
                    stream.publish_checkpoint(previous, entries, [], {object_path: str(local)}, root, stream.TARGET_BUCKET, None)
                self.assertNotIn(stream.ROOT + "/index.json", calls)
            else:
                result = stream.publish_checkpoint(previous, entries, [], {object_path: str(local)}, root, stream.TARGET_BUCKET, None)
                self.assertEqual({entry["key"] for entry in result["files"]}, {"old", "new"})
                self.assertTrue(calls[0].startswith("reader-index/processing/"))
                self.assertEqual(calls[-1], stream.ROOT + "/index.json")
                self.assertEqual(result["files"][0]["processing_roots"], [calls[0]])

    def test_verified_checkpoint_preserves_old_mappings_and_protects_before_upload(self):
        with tempfile.TemporaryDirectory() as self.directory:
            self.publication()

    def test_corrupt_readback_cannot_advance_the_index(self):
        with tempfile.TemporaryDirectory() as self.directory:
            self.publication(corrupt=True)

    def test_changed_parent_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as self.directory:
            self.publication(racing=True)

    def test_workflow_reuses_bucket_path_and_is_in_the_gc_producer_fence(self):
        from scripts.reader_gc_fence import producer_path
        workflow = Path(__file__).parents[1] / ".github/workflows/publish-source-txt.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("inputs.apply == true && 'reader-sidecar'", text)
        self.assertIn("reader-text-plan-{0}", text)
        self.assertIn("--plan-only", text)
        self.assertIn("pilot_text_stream.py", text)
        self.assertIn("build_reader_assets_v2_sidecar.py --apply", text)
        self.assertNotIn("run: python scripts/publish_source_txt_once.py", text)
        self.assertTrue(producer_path(".github/workflows/publish-source-txt.yml"))


if __name__ == "__main__":
    unittest.main()
