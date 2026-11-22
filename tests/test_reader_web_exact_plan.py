import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import pilot_web_stream as producer
from scripts import reader_web_exact_plan as subject


def digest(raw):
    return sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


class ExactWebPlanTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.original = b"\xfforiginal-private-fixture\x00"
        self.html = "<html><p>candidate-private-fixture 中文</p></html>".encode()
        self.old = self.row(b"old html", revision="a" * 40)
        self.old.update(checkpointed_at="2026-11-21T01:00:00+00:00",
                        processing_roots=[f"reader-index/processing/stream-upload-{'b' * 32}.json"])
        self.candidate = self.row(self.html, revision="c" * 40)
        self.manifest = self.manifest_for(self.candidate)
        self.parent = {"version": 1, "kind": subject.KIND, "files": [
            {"key": "org/repo\0untouched.mht", "opaque_metadata": {"tag": True}}, self.old.copy(),
        ], "failures": [{"key": "org/repo\0failed.mht", "error": "failure-private-fixture"}],
            "checkpoint_operation": "d" * 32}
        self.args = {name: self.work / name for name in
                     ("canonical", "old_row", "candidate_row", "manifest", "html", "original")}
        self.sync_inputs()

    def row(self, html, *, revision, original=None):
        repo, path = "org/repo", "dir/原件.mht"
        original = self.original if original is None else original
        source, output = digest(original), digest(html)
        root = f"documents/web/mht/{digest((source + chr(0) + output).encode())}"
        return {"key": f"{repo}\0{path}", "repo": repo, "path": path, "extension": "mht",
                "source_revision": revision, "source_sha256": source, "mode": "document-stream",
                "bucket": subject.BUCKET, "object": f"{root}/document.html",
                "manifest": f"{root}/document-manifest.json", "bytes": len(html), "sha256": output}

    @staticmethod
    def manifest_for(row):
        return {"kind": "web-document-stream", "version": 1, "path": "document.html",
                "source_extension": row["extension"],
                **{k: row[k] for k in ("source_sha256", "bytes", "sha256")}}

    def sync_inputs(self):
        for name, value in (("canonical", self.parent), ("old_row", self.old),
                            ("candidate_row", self.candidate), ("manifest", self.manifest),
                            ("html", self.html), ("original", self.original)):
            self.args[name].write_bytes(value if isinstance(value, bytes) else encode(value))
        self.args["canonical_sha256"] = digest(encode(self.parent))

    def cli(self, *extras):
        argv = [word for name, value in self.args.items() for word in
                ("--" + name.replace("_", "-"), str(value))] + list(extras)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = subject.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_report_retains_old_bundle_and_previews_exactly_one_row(self):
        with patch.object(subject, "merge_observed", wraps=subject.merge_observed) as merge:
            report = subject.plan(**self.args)
        merge.assert_called_once()
        canonical, baseline, entries, failures = merge.call_args.args
        self.assertEqual(canonical, self.parent)
        self.assertEqual(baseline, canonical)
        self.assertEqual(failures, [])
        self.assertTrue(merge.call_args.kwargs["preserve_canonical_rows"])
        self.assertEqual(entries, [{**self.candidate, "source_revision": self.old["source_revision"],
                                    "checkpointed_at": self.old["checkpointed_at"]}])
        self.assertEqual(report["canonical_parent_sha256"], self.args["canonical_sha256"])
        self.assertEqual(report["canonical_index"], "documents/web/mht/index.json")
        self.assertEqual(report["same_source_sha256"], digest(self.original))
        self.assertEqual((report["changed_rows"], report["other_rows_preserved"], report["failures_preserved"]),
                         (1, 1, 1))
        self.assertEqual(report["retained_roots_plan"], [{
            "bucket": subject.BUCKET, "root": self.old["object"].rsplit("/", 1)[0],
            "objects": [self.old["object"], self.old["manifest"]], "html_sha256": self.old["sha256"],
            "html_bytes": self.old["bytes"], "processing_roots": self.old["processing_roots"],
        }])
        self.assertEqual(report["candidate"]["manifest_sha256"], digest(encode(self.manifest)))
        self.assertEqual(report["candidate"]["object"], self.candidate["object"])
        self.assertFalse(report["publication_ready"])
        self.assertFalse(report["retention_verified"])
        self.assertEqual(report["remote_writes"], 0)

    def test_cli_is_read_only_offline_and_metadata_only(self):
        files = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.work.iterdir()}
        with patch("socket.socket.connect", side_effect=AssertionError("network is forbidden")):
            rc, out, err = self.cli()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), subject.plan(**self.args))
        self.assertNotIn("private-fixture", out)
        self.assertNotIn("checkpoint_operation", out)
        self.assertNotIn('"files"', out)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.work.iterdir()}, files)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            self.cli("--apply")
        self.assertEqual(rejected.exception.code, 2)

    def test_canonical_pin_is_over_raw_bytes(self):
        self.args["canonical"].write_bytes(encode(self.parent) + b" ")
        rc, out, err = self.cli()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("canonical raw SHA-256 mismatch", err)
        self.assertNotIn("private-fixture", err)

    def test_rejects_duplicate_json_and_duplicate_index_keys(self):
        self.parent["files"].append(self.old)
        self.sync_inputs()
        with self.assertRaisesRegex(ValueError, "duplicate canonical files key"):
            subject.plan(**self.args)
        self.parent["files"].pop()
        self.parent["failures"] *= 2
        self.sync_inputs()
        with self.assertRaisesRegex(ValueError, "duplicate canonical failures key"):
            subject.plan(**self.args)
        self.parent["failures"].pop()
        self.sync_inputs()
        raw = encode(self.parent).replace(b'"version": 1', b'"version": 1, "version": 1', 1)
        self.args["canonical"].write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "invalid canonical index JSON"):
            subject.plan(**{**self.args, "canonical_sha256": digest(raw)})

    def test_exact_old_row_and_candidate_identity_are_independent_gates(self):
        for field, value in {"bytes": 91, "sha256": "e" * 64, "source_revision": "f" * 40,
                             "checkpointed_at": "different", "processing_roots": []}.items():
            with self.subTest(field=field):
                self.args["old_row"].write_bytes(encode({**self.old, field: value}))
                with self.assertRaisesRegex(ValueError, "exact old row does not match"):
                    subject.plan(**self.args)
        self.sync_inputs()
        for change, error in (({"path": "elsewhere.mht"}, "repo/path key"),
                              ({"repo": "different/repo", "key": "different/repo\0dir/原件.mht"}, "exact old repo/path"),
                              ({"bucket": "different/bucket"}, "web stream metadata"),
                              ({"object": self.old["object"]}, "exact immutable paths"),
                              ({"manifest": self.old["manifest"]}, "exact immutable paths")):
            with self.subTest(change=change):
                self.args["candidate_row"].write_bytes(encode({**self.candidate, **change}))
                with self.assertRaisesRegex(ValueError, error):
                    subject.plan(**self.args)

    def test_rejects_moved_source_and_noop_even_with_coherent_artifacts(self):
        self.candidate = self.row(self.html, revision="x", original=b"different original")
        self.manifest = self.manifest_for(self.candidate)
        self.sync_inputs()
        with self.assertRaisesRegex(ValueError, "original SHA-256 differs"):
            subject.plan(**self.args)
        self.html = b"old html"
        self.candidate = self.row(self.html, revision="x")
        self.manifest = self.manifest_for(self.candidate)
        self.sync_inputs()
        with self.assertRaisesRegex(ValueError, "not a new immutable"):
            subject.plan(**self.args)

    def test_rejects_original_html_and_manifest_tampering(self):
        for field, value in {"kind": "other", "version": True, "path": "../document.html", "bytes": 1,
                             "source_extension": "html", "source_sha256": "0" * 64, "sha256": "0" * 64}.items():
            with self.subTest(field=field):
                self.args["manifest"].write_bytes(encode({**self.manifest, field: value}))
                with self.assertRaisesRegex(ValueError, "manifest does not match"):
                    subject.plan(**self.args)
        self.sync_inputs()
        for field, raw, error in (("html", self.html[:-1], "HTML size or SHA-256"),
                                  ("html", b"X" + self.html[1:], "HTML size or SHA-256"),
                                  ("original", b"different", "original SHA-256")):
            with self.subTest(field=field, size=len(raw)):
                self.args[field].write_bytes(raw)
                with self.assertRaisesRegex(ValueError, error):
                    subject.plan(**self.args)
                self.sync_inputs()

    def test_strict_utf8_gate_after_matching_size_and_hash(self):
        self.html = b"<p>candidate-private-fixture\xc0\xaf</p>"
        self.candidate = self.row(self.html, revision="x")
        self.manifest = self.manifest_for(self.candidate)
        self.sync_inputs()
        rc, out, err = self.cli()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("HTML is not strict UTF-8", err)
        self.assertNotIn("private-fixture", err)

    def test_rejects_failure_removal_by_real_merge(self):
        self.parent["failures"].append({"key": self.old["key"], "source_revision": self.old["source_revision"]})
        self.sync_inputs()
        with self.assertRaisesRegex(ValueError, "no failure changes"):
            subject.plan(**self.args)

    def test_checks_merge_postcondition_for_missing_and_changed_unrelated_rows(self):
        real_merge = subject.merge_observed
        for change in (lambda rows: rows.pop(), lambda rows: rows[-1]["opaque_metadata"].update(tag=1)):
            def regress(*args, **kwargs):
                merged = real_merge(*args, **kwargs)
                change(merged["files"])
                return merged

            with patch.object(subject, "merge_observed", side_effect=regress):
                with self.assertRaisesRegex(ValueError, "only the exact selected row"):
                    subject.plan(**self.args)

    def test_byte_limits_and_nonregular_file(self):
        for name, constant in (("canonical", "MAX_CANONICAL"), ("old_row", "MAX_ROW"),
                               ("candidate_row", "MAX_ROW"), ("manifest", "MAX_MANIFEST"),
                               ("html", "MAX_HTML"), ("original", "MAX_ORIGINAL")):
            with self.subTest(name=name), patch.object(subject, constant, 1):
                with self.assertRaisesRegex(ValueError, "exceeds byte limit"):
                    subject.plan(**self.args)
        fifo = self.work / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "local regular file"):
            subject.plan(**{**self.args, "html": fifo})

    def test_accepts_actual_producer_artifacts_offline(self):
        original = b'<p align="center">A local fixture</p>'
        item = {"repo": "org/repo", "path": "path/源.html", "extension": "html", "revision": "e" * 40}
        with patch.object(producer, "download", side_effect=lambda url, target, token: target.write_bytes(original)), \
                patch("socket.socket.connect", side_effect=AssertionError("network is forbidden")):
            old, _ = producer.build_one(item, self.work / "baseline", None, producer.TARGET_BUCKET)
            new, files = producer.build_one(item, self.work / "candidate-build", None, producer.TARGET_BUCKET,
                                            web_typography=True)
        self.old = old
        self.parent["files"] = [deepcopy(old)]
        self.original = original
        self.candidate = new
        self.html = Path(files[new["object"]]).read_bytes()
        self.manifest = Path(files[new["manifest"]]).read_bytes()
        self.sync_inputs()
        report = subject.plan(**self.args)
        self.assertEqual(report["canonical_index"], "documents/web/html/index.json")
        self.assertEqual(report["changed_rows"], 1)
        self.assertEqual(report["retained_roots_plan"][0]["objects"], [old["object"], old["manifest"]])


if __name__ == "__main__":
    unittest.main()
