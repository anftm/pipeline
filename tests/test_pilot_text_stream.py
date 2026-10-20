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
from scripts.text_encoding import PROFILE, REVIEWED_SOURCE_REPAIRS, TextEncodingError, normalize_utf8


def repair_fixture(raw, old_span, replacement):
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "offset": raw.index(old_span),
            "original_text": old_span.decode("utf-8"), "replacement_text": replacement,
            "reference_url": "https://example.invalid/review.html", "reference_sha256": "a" * 64}


def suffix_repair_fixture(raw, offset_chars, replacement):
    text = raw.decode("utf-8")
    suffix = text[offset_chars:]
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "mode": "suffix-strip",
            "offset_chars": offset_chars, "original_suffix_characters": len(suffix),
            "original_suffix_sha256": hashlib.sha256(suffix.encode()).hexdigest(),
            "replacement_text": replacement,
            "replacement_sha256": hashlib.sha256(replacement.encode()).hexdigest(),
            "reference_url": "https://example.invalid/review.html", "reference_sha256": "a" * 64,
            "reference_kind": "fixture",
            "image_spans": [{"start": offset_chars, "end": len(text), "characters": len(suffix)}]}


def control_repair_fixture(raw, codec, offsets):
    text = raw.decode(codec)
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "mode": "control-strip",
            "source_encoding": codec, "control_offsets": offsets,
            "control_codepoints": [text[offset] for offset in offsets],
            "reference_url": "https://example.invalid/review.html", "reference_sha256": "a" * 64,
            "reference_kind": "fixture"}


def marker_repair_fixture(raw, marker, offsets):
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "mode": "legacy-marker-strip",
            "source_encoding": "gb18030", "marker_hex": marker.hex(),
            "marker_count": len(offsets), "marker_offsets": offsets,
            "reference_url": "https://example.invalid/review.html", "reference_sha256": "a" * 64,
            "reference_kind": "fixture"}


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

    def test_windows_936_euro_is_explicit_strict_and_byte_reversible(self):
        raw = "中文".encode("gbk") + b"\x80" + b"\x81\x80" + b"\r\n" + b"\x80"
        expected = "中文€" + b"\x81\x80".decode("gbk") + "\r\n€"
        output, metadata = normalize_utf8(raw, encoding="windows-936")
        self.assertEqual(output, codecs.BOM_UTF8 + expected.encode())
        self.assertEqual(b"\x80".join(part.encode("gbk") for part in output.decode("utf-8-sig").split("€")), raw)
        self.assertEqual(metadata["source_encoding"], "windows-936")
        self.assertTrue(metadata["source_roundtrip_verified"])
        self.assertEqual(metadata["encoding_decision"], "explicit")
        with self.assertRaises(UnicodeError):
            normalize_utf8(raw, encoding="gbk")
        for damaged in (b"\x81", b"\xa3 ", b"\xff\x80", b"\x81\x30\x80"):
            with self.subTest(raw=damaged), self.assertRaises(UnicodeError):
                normalize_utf8(damaged, encoding="windows-936")

    def test_reviewed_sub_control_is_preserved_without_truncating_later_text(self):
        text = "前文\r\n上午八\x1a时四十分\n完整后文"
        raw = text.encode("gb18030")
        with self.assertRaises(TextEncodingError):
            normalize_utf8(raw)
        output, metadata = normalize_utf8(raw, encoding="gb18030", preserve_controls={"U+001A": 1})
        self.assertEqual(output.decode("utf-8-sig"), text)
        self.assertEqual(output.decode("utf-8-sig").encode("gb18030"), raw)
        self.assertEqual(metadata["preserved_controls"], {"U+001A": 1})
        self.assertTrue(metadata["source_roundtrip_verified"])
        for options in ({"U+001A": 2}, {"U+0000": 1}, {"U+001A": True}, {"U+001A": 0}, [], "U+001A"):
            with self.subTest(options=options), self.assertRaises(TextEncodingError):
                normalize_utf8(raw, encoding="gb18030", preserve_controls=options)
        with self.assertRaisesRegex(TextEncodingError, "explicit encoding"):
            normalize_utf8(raw, preserve_controls={"U+001A": 1})
        with self.assertRaises(TextEncodingError):
            normalize_utf8(raw + b"\0", encoding="gb18030", preserve_controls={"U+001A": 1})

    def test_one_reviewed_stray_prefix_byte_does_not_discard_the_first_paragraph(self):
        header = "广告\r\n".encode("gb18030")
        body = "完整第一段手机。\n\n完整末段。\r\n".encode("gb18030")
        raw = header + b"\xcd" + body
        offset = len(header)
        with self.assertRaises(UnicodeError):
            normalize_utf8(raw, encoding="gb18030")
        output, metadata = normalize_utf8(raw, encoding="gb18030", remove_prefix_byte={"offset": offset, "hex": "cd"})
        self.assertEqual(output.decode("utf-8-sig"), (header + body).decode("gb18030"))
        retained = output.decode("utf-8-sig").encode("gb18030")
        self.assertEqual(retained[:offset] + b"\xcd" + retained[offset:], raw)
        self.assertEqual(metadata["removed_prefix_byte"], {"offset": offset, "hex": "cd"})
        self.assertEqual(metadata["normalization_recipe"], "strict-decode-with-reviewed-prefix-byte-removal-v1")
        self.assertTrue(metadata["source_roundtrip_verified"])
        with self.assertRaises(UnicodeError):
            normalize_utf8(raw + b"\xa3", encoding="gb18030", remove_prefix_byte={"offset": offset, "hex": "cd"})

    def test_prefix_byte_repair_cannot_guess_delete_ranges_or_skip_later_bad_bytes(self):
        raw = b"header\n\xcd" + "完整正文".encode("gb18030")
        for removal in ({"offset": 7, "hex": "ce"}, {"offset": 7, "hex": "cdce"},
                        {"offset": -1, "hex": "cd"}, {"offset": True, "hex": "cd"},
                        {"offset": 4096, "hex": "cd"}, {"offset": len(raw), "hex": "cd"},
                        {"offset": 7, "hex": "cd", "length": 20}, None):
            with self.subTest(removal=removal), self.assertRaises((UnicodeError, TextEncodingError)):
                normalize_utf8(raw, encoding="gb18030", remove_prefix_byte=removal)
        with self.assertRaises(TextEncodingError):
            normalize_utf8(raw, remove_prefix_byte={"offset": 7, "hex": "cd"})

    def test_exact_reviewed_source_repair_preserves_every_other_character(self):
        damaged, replacement = "穹\0瘢\0\0?/p>", "民服务？"
        text = "前文\r\n" + damaged + "literal\ufffd完整后文\n"
        for bom in (b"", codecs.BOM_UTF8):
            raw = bom + text.encode()
            fixture = repair_fixture(raw, damaged.encode(), replacement)
            with self.subTest(bom=bool(bom)), patch.dict(REVIEWED_SOURCE_REPAIRS, {"fixture": fixture}):
                with self.assertRaises(TextEncodingError):
                    normalize_utf8(raw)
                output, metadata = normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="fixture")
                self.assertEqual(output.decode("utf-8-sig"), text.replace(damaged, replacement))
                self.assertEqual(metadata["reviewed_source_repair"], "fixture")
                self.assertEqual(metadata["replaced_source_span"]["original_text"], damaged)
                self.assertEqual(metadata["normalization_recipe"], "strict-utf8-reviewed-source-repair-v1")
                self.assertTrue(metadata["source_roundtrip_verified"])
                retained = output.decode("utf-8-sig").encode(metadata["source_encoding"])
                offset = fixture["offset"]
                self.assertEqual(retained[:offset] + damaged.encode() + retained[offset + len(replacement.encode()):], raw)

    def test_reviewed_suffix_strip_keeps_text_and_captions_only(self):
        prefix = "完整前文\n\n## 图一说明\n\n"
        image1 = "\x01\x02JFIF binary-ish payload\x1f"
        caption = "==========\n\n## 图二说明\n\n"
        image2 = "\x03\x04JFIF second payload\x1e"
        text = prefix + image1 + caption + image2
        raw = text.encode()
        offset = len(prefix)
        fixture = suffix_repair_fixture(raw, offset, caption)
        with patch.dict(REVIEWED_SOURCE_REPAIRS, {"suffix-fixture": fixture}):
            with self.assertRaises(TextEncodingError):
                normalize_utf8(raw)
            output, metadata = normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="suffix-fixture")
            self.assertEqual(output.decode("utf-8-sig"), prefix + caption)
            self.assertEqual(metadata["reviewed_source_repair"], "suffix-fixture")
            self.assertEqual(metadata["normalization_recipe"], "strict-utf8-reviewed-source-suffix-strip-v1")
            self.assertTrue(metadata["source_roundtrip_verified"])
            self.assertEqual(metadata["replaced_source_span"]["offset_chars"], offset)
            self.assertEqual(metadata["replaced_source_span"]["original_characters"], len(text) - offset)
            self.assertEqual(metadata["replaced_source_span"]["replacement_text"], caption)
            self.assertEqual(metadata["replaced_source_span"]["image_spans"],
                             [{"start": offset, "end": len(text), "characters": len(text) - offset}])
            cleaned = output.decode("utf-8-sig")
            self.assertEqual(cleaned[:offset] + text[offset:], text)
            with self.assertRaisesRegex(TextEncodingError, "SHA-256"):
                normalize_utf8(raw + b"x", encoding="utf-8", reviewed_source_repair="suffix-fixture")
            for bad_offset in (offset + 1, len(text) + 1, -1, True):
                with self.subTest(bad_offset=bad_offset), self.assertRaises(TextEncodingError):
                    with patch.dict(REVIEWED_SOURCE_REPAIRS,
                                    {"suffix-fixture": {**fixture, "offset_chars": bad_offset}}):
                        normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="suffix-fixture")
            with patch.dict(REVIEWED_SOURCE_REPAIRS,
                            {"suffix-fixture": {**fixture, "replacement_sha256": "0" * 64}}):
                with self.assertRaisesRegex(TextEncodingError, "replacement"):
                    normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="suffix-fixture")
            with patch.dict(REVIEWED_SOURCE_REPAIRS,
                            {"suffix-fixture": {**fixture, "replacement_text": "bad\x00"}}):
                with self.assertRaises(TextEncodingError):
                    normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="suffix-fixture")
            for encoding in (None, "utf-8-sig", "gb18030"):
                with self.subTest(encoding=encoding), self.assertRaises(TextEncodingError):
                    normalize_utf8(raw, encoding=encoding, reviewed_source_repair="suffix-fixture")

    def test_reviewed_control_strip_removes_only_exact_controls(self):
        text = "完整前文\x1e中间\x0e完整后文"
        raw = text.encode()
        offsets = [text.index("\x1e"), text.index("\x0e")]
        fixture = control_repair_fixture(raw, "utf-8", offsets)
        with patch.dict(REVIEWED_SOURCE_REPAIRS, {"control-fixture": fixture}):
            with self.assertRaises(TextEncodingError):
                normalize_utf8(raw)
            output, metadata = normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="control-fixture")
            self.assertEqual(output.decode("utf-8-sig"), text.replace("\x1e", "").replace("\x0e", ""))
            self.assertEqual(metadata["source_encoding"], "utf-8")
            self.assertEqual(metadata["normalization_recipe"], "strict-reviewed-control-strip-v1")
            self.assertEqual(metadata["replaced_source_span"]["control_offsets"], offsets)
            self.assertEqual(metadata["replaced_source_span"]["removed_controls"], 2)
            self.assertTrue(metadata["source_roundtrip_verified"])
            with self.assertRaisesRegex(TextEncodingError, "SHA-256"):
                normalize_utf8(raw + b"x", encoding="utf-8", reviewed_source_repair="control-fixture")
            for bad in ({**fixture, "control_offsets": [offsets[0] + 1, offsets[1]]},
                        {**fixture, "control_codepoints": ["\x1f", "\x0e"]},
                        {**fixture, "control_offsets": [offsets[0]]}):
                with self.subTest(bad=bad), self.assertRaises(TextEncodingError):
                    with patch.dict(REVIEWED_SOURCE_REPAIRS, {"control-fixture": bad}):
                        normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="control-fixture")
            with self.assertRaises(TextEncodingError):
                normalize_utf8(raw, encoding="gb18030", reviewed_source_repair="control-fixture")

        legacy = "中文\x10正文"
        legacy_raw = legacy.encode("gb18030")
        legacy_fixture = control_repair_fixture(legacy_raw, "gb18030", [legacy.index("\x10")])
        with patch.dict(REVIEWED_SOURCE_REPAIRS, {"legacy-control-fixture": legacy_fixture}):
            output, metadata = normalize_utf8(legacy_raw, encoding="gb18030",
                                              reviewed_source_repair="legacy-control-fixture")
            self.assertEqual(output.decode("utf-8-sig"), legacy.replace("\x10", ""))
            self.assertEqual(metadata["source_encoding"], "gb18030")
            self.assertEqual(metadata["replaced_source_span"]["removed_controls"], 1)
            with self.assertRaises(TextEncodingError):
                normalize_utf8(legacy_raw, encoding="utf-8",
                               reviewed_source_repair="legacy-control-fixture")

    def test_reviewed_legacy_marker_strip_removes_only_exact_markers(self):
        marker = bytes.fromhex("f8fa0702cb35f501")
        text = "工农兵学员正文一" + marker.decode("gb18030", errors="ignore") + "正文二"
        # Build raw bytes directly so the fixture marker positions are exact.
        raw = "工农兵学员正文一".encode("gb18030") + marker + "正文二".encode("gb18030")
        offsets = [len("工农兵学员正文一".encode("gb18030"))]
        fixture = marker_repair_fixture(raw, marker, offsets)
        with patch.dict(REVIEWED_SOURCE_REPAIRS, {"marker-fixture": fixture}):
            with self.assertRaises((UnicodeError, TextEncodingError)):
                normalize_utf8(raw)
            output, metadata = normalize_utf8(raw, encoding="gb18030", reviewed_source_repair="marker-fixture")
            self.assertEqual(output.decode("utf-8-sig"), "工农兵学员正文一正文二")
            self.assertEqual(metadata["source_encoding"], "gb18030")
            self.assertEqual(metadata["normalization_recipe"], "strict-legacy-binary-marker-strip-v1")
            self.assertEqual(metadata["replaced_source_span"]["marker_count"], 1)
            self.assertEqual(metadata["replaced_source_span"]["marker_offsets"], offsets)
            self.assertTrue(metadata["source_roundtrip_verified"])
            with self.assertRaisesRegex(TextEncodingError, "SHA-256"):
                normalize_utf8(raw + b"x", encoding="gb18030", reviewed_source_repair="marker-fixture")
            for bad in ({**fixture, "marker_offsets": [offsets[0] + 1]},
                        {**fixture, "marker_hex": "00"},
                        {**fixture, "marker_count": 2}):
                with self.subTest(bad=bad), self.assertRaises(TextEncodingError):
                    with patch.dict(REVIEWED_SOURCE_REPAIRS, {"marker-fixture": bad}):
                        normalize_utf8(raw, encoding="gb18030", reviewed_source_repair="marker-fixture")
            with self.assertRaises(TextEncodingError):
                normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="marker-fixture")

    def test_source_repair_refuses_unknown_recipes_byte_drift_and_other_encodings(self):
        raw = b"A\0B"
        fixture = repair_fixture(raw, b"\0", "复原")
        with patch.dict(REVIEWED_SOURCE_REPAIRS, {"fixture": fixture}):
            for recipe in (None, "not-reviewed", {}, True):
                with self.subTest(recipe=recipe), self.assertRaises(TextEncodingError):
                    normalize_utf8(raw, encoding="utf-8", reviewed_source_repair=recipe)
            for encoding in (None, "gb18030", "utf-16", "cp1252"):
                with self.subTest(encoding=encoding), self.assertRaises(TextEncodingError):
                    normalize_utf8(raw, encoding=encoding, reviewed_source_repair="fixture")
            with self.assertRaisesRegex(TextEncodingError, "SHA-256"):
                normalize_utf8(raw + b"changed", encoding="utf-8", reviewed_source_repair="fixture")
            for options in ({"preserve_controls": {"U+001A": 1}}, {"remove_prefix_byte": {"offset": 0, "hex": "41"}}):
                with self.subTest(options=options), self.assertRaises(TextEncodingError):
                    normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="fixture", **options)
        for damaged in (b"A\0\xff", b"A\0\x01"):
            with patch.dict(REVIEWED_SOURCE_REPAIRS, {"fixture": repair_fixture(damaged, b"\0", "复原")}):
                with self.subTest(damaged=damaged), self.assertRaises((UnicodeError, TextEncodingError)):
                    normalize_utf8(damaged, encoding="utf-8", reviewed_source_repair="fixture")
        with patch.dict(REVIEWED_SOURCE_REPAIRS, {"fixture": {**fixture, "offset": 0}}):
            with self.assertRaisesRegex(TextEncodingError, "span"):
                normalize_utf8(raw, encoding="utf-8", reviewed_source_repair="fixture")


class TextStreamPublicationTests(unittest.TestCase):
    def test_article_repairs_have_distinct_complete_source_and_reference_identities(self):
        first = REVIEWED_SOURCE_REPAIRS["mlmrl-1967-02-01-mojibake-v1"]
        second = REVIEWED_SOURCE_REPAIRS["mlmrl-1967-04-16-mojibake-v1"]
        third = REVIEWED_SOURCE_REPAIRS["mlmrl-1967-07-21-mojibake-v1"]
        fourth = REVIEWED_SOURCE_REPAIRS["mlmrl-1967-09-01-mojibake-v1"]
        fifth = REVIEWED_SOURCE_REPAIRS["mlmrl-1968-03-21-mojibake-v1"]
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])
        self.assertNotEqual(first["reference_sha256"], second["reference_sha256"])
        self.assertEqual(second["source_sha256"], "7088b4a442c02e818ee63a26c9d30952fb1a78ce6fb35afe57aa75f071449f27")
        self.assertEqual(second["offset"], 9761)
        self.assertEqual(second["original_text"].count("\0"), 22)
        self.assertEqual(len(second["original_text"].encode()), 328)
        self.assertTrue(second["reference_url"].endswith("/088.html"))
        self.assertEqual(third["source_sha256"], "d31061576c29a154388f04ebd2219f66d2cfef9921e7a3f2154ac9e825e1aeb3")
        self.assertEqual(third["offset"], 9887)
        self.assertEqual(third["original_text"].count("\0"), 9)
        self.assertEqual(len(third["original_text"].encode()), 106)
        self.assertEqual(len(third["replacement_text"].encode()), 125)
        self.assertNotIn("\0", third["replacement_text"])
        self.assertIn("派都在游行，从这里可以看出", third["replacement_text"])
        self.assertTrue(third["reference_url"].endswith("/100.html"))
        self.assertEqual(third["reference_sha256"], "0b7ff0cdfe2f7d91fa86afa431f53f987b1565ad127439920df576b526b15d0f")
        self.assertEqual(fourth["source_sha256"], "dd446bcf91636ffc0b8254311191641c9fd5bdf306a80e1cce7005880d72a32c")
        self.assertEqual(fourth["offset"], 9831)
        self.assertEqual(fourth["original_text"].count("\0"), 18)
        self.assertEqual(len(fourth["original_text"].encode()), 256)
        self.assertNotIn("\0", fourth["replacement_text"])
        self.assertIn("打倒你们，还有韩爱晶", fourth["replacement_text"])
        self.assertTrue(fourth["reference_url"].endswith("/104.html"))
        self.assertEqual(fourth["reference_sha256"], "ccfe1bd8d5d6cc6fffb8c62d6c503ab1824be75c5a8e0c51fedb66b056a9d83e")
        self.assertEqual(fifth["source_sha256"], "6c1dc88c70a92b868186d19d9a4a8ce19b5a63dfd0961cfd80015974c66cbd11")
        self.assertEqual(fifth["offset"], 16097)
        self.assertEqual(fifth["original_text"].count("\0"), 55)
        self.assertEqual(len(fifth["original_text"].encode()), 586)
        self.assertNotIn("\0", fifth["replacement_text"])
        self.assertTrue(fifth["replacement_text"].startswith("惕，防止敌人进攻"))
        self.assertTrue(fifth["reference_url"].endswith("/125.html"))
        self.assertEqual(fifth["reference_sha256"], "90daefc2119814ff52a0dda96e1ca1d5eb93efb84b799e6b6cdd349f997867ea")
        for recipe, repair in (("mlmrl-1967-02-01-mojibake-v1", first), ("mlmrl-1967-04-16-mojibake-v1", second),
                               ("mlmrl-1967-07-21-mojibake-v1", third), ("mlmrl-1967-09-01-mojibake-v1", fourth),
                               ("mlmrl-1968-03-21-mojibake-v1", fifth)):
            # Even a matching damaged span never authorizes a different whole file.
            fake = b"prefix" + repair["original_text"].encode() + b"suffix"
            with self.subTest(recipe=recipe), self.assertRaisesRegex(TextEncodingError, "SHA-256"):
                normalize_utf8(fake, encoding="utf-8", reviewed_source_repair=recipe)

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

    def test_reviewed_control_override_is_bound_to_original_bytes_and_reported(self):
        item = {"repo": "VoiceOfML/Test", "path": "reviewed.txt", "extension": "txt", "revision": "a" * 40}
        raw = "完整前文\x1a完整后文".encode("gb18030")
        options = {"encoding": "gb18030", "preserve_controls": {"U+001A": 1}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stream, "download", side_effect=lambda url, target, token: target.write_bytes(raw)):
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "approved selection"):
                stream.build_one(item, root / "wrong", None, stream.TARGET_BUCKET, options, expected_sha256="0" * 64)
            entry, files = stream.build_one(item, root / "correct", None, stream.TARGET_BUCKET, options,
                                            expected_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(Path(files[entry["object"]]).read_bytes().decode("utf-8-sig").encode("gb18030"), raw)
            self.assertEqual(entry["preserved_controls"], {"U+001A": 1})
            self.assertTrue(entry["source_roundtrip_verified"])

    def test_reviewed_prefix_repair_is_not_misreported_as_unchanged_utf8(self):
        item = {"repo": "VoiceOfML/Test", "path": "reviewed.txt", "extension": "txt", "revision": "a" * 40}
        raw = b"\xffComplete UTF-8 body\r\n"
        options = {"encoding": "utf-8", "remove_prefix_byte": {"offset": 0, "hex": "ff"}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stream, "download", side_effect=lambda url, target, token: target.write_bytes(raw)):
            entry, files = stream.build_one(item, Path(directory), None, stream.TARGET_BUCKET, options,
                                            skip_utf8=True, expected_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(Path(files[entry["object"]]).read_bytes(), codecs.BOM_UTF8 + raw[1:])
            self.assertEqual(entry["removed_prefix_byte"], {"offset": 0, "hex": "ff"})

    def test_reviewed_source_repair_requires_sha_and_retains_original_provenance(self):
        item = {"repo": "VoiceOfML/Test", "path": "reviewed.txt", "extension": "txt", "revision": "a" * 40}
        damaged, replacement = "穹\0瘢\0?/p>", "复原句。"
        raw = ("完整前文" + damaged + "完整后文\r\n").encode()
        options = {"encoding": "utf-8", "reviewed_source_repair": "fixture"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(REVIEWED_SOURCE_REPAIRS, {"fixture": repair_fixture(raw, damaged.encode(), replacement)}), \
                patch.object(stream, "download", side_effect=lambda url, target, token: target.write_bytes(raw)):
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "approved source checksum"):
                stream.build_one(item, root / "unapproved", None, stream.TARGET_BUCKET, options, skip_utf8=True)
            with self.assertRaisesRegex(ValueError, "approved selection"):
                stream.build_one(item, root / "wrong", None, stream.TARGET_BUCKET, options,
                                 expected_sha256="0" * 64)
            entry, files = stream.build_one(item, root / "approved", None, stream.TARGET_BUCKET, options,
                                            skip_utf8=True, expected_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(Path(files[entry["object"]]).read_bytes(), codecs.BOM_UTF8 + raw.replace(damaged.encode(), replacement.encode()))
            self.assertEqual(entry["reviewed_source_repair"], "fixture")
            retained = entry["retained_source"]
            self.assertEqual(Path(files[retained["path"]]).read_bytes(), raw)
            self.assertEqual(retained["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(retained["role"], "provenance")
            self.assertTrue(retained["path"].startswith(entry["object"].rsplit("/", 1)[0] + "/"))
            self.assertEqual(len(files), 3)

    def test_override_apply_requires_exact_checksum_selection_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "VoiceOfML/Test\0reviewed.txt"
            source = root / "source.json"
            source.write_text(json.dumps([{"Repo": "VoiceOfML/Test", "File": "reviewed", "Extension": "txt", "Folder": []}]))
            revisions = root / "revisions.json"; revisions.write_text(json.dumps({"VoiceOfML/Test": "a" * 40}))
            overrides = root / "overrides.json"; overrides.write_text(json.dumps({key: "windows-936"}))
            args = SimpleNamespace(search_data=source, revisions=revisions, extensions=["txt"], limit=0,
                checkpoint_size=64, bucket=stream.TARGET_BUCKET, apply=True, plan_only=False,
                force=False, retry_failures=True, encoding_overrides=overrides, report=root / "report.json")
            with patch.object(stream, "parse_args", return_value=args), \
                    patch.object(stream, "existing_index", return_value={"version": 1, "kind": "text-stream-index", "files": []}), \
                    patch.object(stream, "download") as fetch, patch.dict(os.environ, {"HF_TOKEN": "test"}):
                with self.assertRaisesRegex(ValueError, "source-SHA selection"):
                    stream.main()
                fetch.assert_not_called()

    def test_override_schema_rejects_unreviewed_control_and_replacement_options(self):
        for value in ({"encoding": "gbk", "errors": "replace"}, {"encoding": "gbk", "preserve_controls": {"U+0000": 1}},
                      {"encoding": "gbk", "preserve_controls": {"U+001A": True}}, {}, [], "",
                      {"encoding": "utf-8", "remove_nul_bytes": [1]},
                      {"encoding": "utf-8", "reviewed_source_repair": "unreviewed"},
                      {"encoding": "utf-8", "reviewed_source_repair": {}},
                      {"encoding": "utf-8", "reviewed_source_repair": "mlmrl-1967-02-01-mojibake-v1", "preserve_controls": {"U+001A": 1}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                stream.encoding_options(value)

    def test_scoped_reviewed_retry_clears_only_the_two_recovered_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bodies = {"controls.txt": "完整前文\x1a完整后文".encode("gb18030"),
                      "windows.txt": "完整中文".encode("gbk") + b"\x80"}
            prefix = "VoiceOfML/Test\0"
            source = root / "source.json"
            source.write_text(json.dumps([{"Repo": "VoiceOfML/Test", "File": name[:-4], "Extension": "txt", "Folder": []}
                                          for name in bodies]))
            revisions = root / "revisions.json"; revisions.write_text(json.dumps({"VoiceOfML/Test": "a" * 40}))
            selection = root / "selection.json"
            selection.write_text(json.dumps({prefix + name: hashlib.sha256(raw).hexdigest() for name, raw in bodies.items()}))
            overrides = root / "overrides.json"
            overrides.write_text(json.dumps({prefix + "controls.txt": {"encoding": "gb18030", "preserve_controls": {"U+001A": 1}},
                                             prefix + "windows.txt": "windows-936"}))
            untouched = {"key": prefix + "damaged.txt", "object": "original-damaged-mapping.txt"}
            remaining = {"key": untouched["key"], "source_revision": "a" * 40, "profile": PROFILE, "error": "damaged original"}
            stored = [{"version": 1, "kind": "text-stream-index", "files": [untouched],
                       "failures": [remaining, *[{"key": prefix + name, "source_revision": "a" * 40, "profile": PROFILE}
                                                 for name in bodies]]}]
            objects = {}
            def upload(bucket, additions, token):
                for local, remote in additions:
                    objects[remote] = Path(local).read_bytes()
                    if remote == stream.ROOT + "/index.json": stored[0] = json.loads(objects[remote])
            def read(remote, token, bucket):
                if remote not in objects: raise FileNotFoundError(remote)
                return objects[remote]
            args = SimpleNamespace(search_data=source, revisions=revisions, extensions=["txt"], limit=0,
                checkpoint_size=64, bucket=stream.TARGET_BUCKET, apply=True, plan_only=False,
                force=False, retry_failures=True, encoding_overrides=overrides, selection=selection, report=root / "report.json")
            with patch.object(stream, "parse_args", return_value=args), \
                    patch.object(stream, "existing_index", side_effect=lambda *args: stored[0]), \
                    patch.object(stream, "download", side_effect=lambda url, target, token: target.write_bytes(bodies[url.rsplit('/', 1)[1]])), \
                    patch.object(stream, "batch_bucket_files_with_retry", side_effect=upload), \
                    patch.object(stream, "read_bytes", side_effect=read), patch.dict(os.environ, {"HF_TOKEN": "test"}):
                self.assertEqual(stream.main(), 0)
            report = json.loads(args.report.read_text())
            self.assertEqual(report["published"], 2)
            self.assertEqual(report["unresolved_failures"], 0)
            self.assertEqual(stored[0]["failures"], [remaining])
            self.assertIn(untouched, stored[0]["files"])
            self.assertEqual(report["converted_sources"][0]["preserved_controls"], {"U+001A": 1})

    def test_exact_source_repair_retry_publishes_one_body_and_keeps_other_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = "VoiceOfML/Test\0"
            key = prefix + "reviewed.txt"
            damaged, replacement = "\0穹瘢?/p>", "复原句。"
            raw = ("完整原文" + damaged + "完整末尾\n").encode()
            options = {"encoding": "utf-8", "reviewed_source_repair": "fixture"}
            source = root / "source.json"; source.write_text(json.dumps([
                {"Repo": "VoiceOfML/Test", "File": "reviewed", "Extension": "txt", "Folder": []}]))
            revisions = root / "revisions.json"; revisions.write_text(json.dumps({"VoiceOfML/Test": "a" * 40}))
            selection = root / "selection.json"; selection.write_text(json.dumps({key: hashlib.sha256(raw).hexdigest()}))
            overrides = root / "overrides.json"; overrides.write_text(json.dumps({key: options}))
            remaining = {"key": prefix + "other.txt", "source_revision": "a" * 40, "profile": PROFILE, "error": "unreviewed damage"}
            old_other = {"key": remaining["key"], "object": "unchanged.txt"}
            stored = [{"version": 1, "kind": "text-stream-index", "files": [old_other],
                       "failures": [remaining, {"key": key, "source_revision": "a" * 40, "profile": PROFILE, "error": "NUL"}]}]
            objects = {}
            def upload(bucket, additions, token):
                for local, remote in additions:
                    objects[remote] = Path(local).read_bytes()
                    if remote == stream.ROOT + "/index.json": stored[0] = json.loads(objects[remote])
            def read(remote, token, bucket):
                if remote not in objects: raise FileNotFoundError(remote)
                return objects[remote]
            args = SimpleNamespace(search_data=source, revisions=revisions, extensions=["txt"], limit=1,
                checkpoint_size=64, bucket=stream.TARGET_BUCKET, apply=True, plan_only=False,
                force=False, retry_failures=True, encoding_overrides=overrides, selection=selection, report=root / "report.json")
            with patch.object(stream, "parse_args", return_value=args), \
                    patch.dict(REVIEWED_SOURCE_REPAIRS, {"fixture": repair_fixture(raw, damaged.encode(), replacement)}), \
                    patch.object(stream, "existing_index", side_effect=lambda *args: stored[0]), \
                    patch.object(stream, "download", side_effect=lambda url, target, token: target.write_bytes(raw)), \
                    patch.object(stream, "batch_bucket_files_with_retry", side_effect=upload), \
                    patch.object(stream, "read_bytes", side_effect=read), patch.dict(os.environ, {"HF_TOKEN": "test"}):
                self.assertEqual(stream.main(), 0)
            report = json.loads(args.report.read_text())
            self.assertEqual(report["published"], 1)
            self.assertEqual(report["unchanged_utf8"], [])
            self.assertEqual(stored[0]["failures"], [remaining])
            self.assertIn(old_other, stored[0]["files"])
            retained = report["converted_sources"][0]["retained_source"]
            self.assertEqual(objects[retained["path"]], raw)
            self.assertEqual(report["converted_sources"][0]["reviewed_source_repair"], "fixture")
            self.assertEqual(report["converted_sources"][0]["replaced_source_span"]["replacement_text"], replacement)

    def test_cleanup_original_stays_in_the_streamed_bucket_reference_graph(self):
        from scripts.reader_gc_stream import graph_json
        from scripts.reader_gc_graph import ReferenceGraph
        original = "documents/text/txt/" + "a" * 64 + "/original-" + "b" * 64 + ".bin"
        document = "documents/text/txt/" + "a" * 64 + "/document.txt"
        payload = {"version": 1, "kind": "text-stream-index", "files": [{
            "key": "VoiceOfML/Test\0reviewed.txt", "repo": "VoiceOfML/Test", "path": "reviewed.txt",
            "object": document, "bucket": stream.TARGET_BUCKET,
            "retained_source": {"bucket": stream.TARGET_BUCKET, "path": original,
                                "bytes": 10, "sha256": "b" * 64, "role": "provenance"}}]}
        parsed, _digest = graph_json([json.dumps(payload).encode()])
        graph = ReferenceGraph(None)
        graph.files = {stream.TARGET_BUCKET: {document, original}}
        graph.ordered = {stream.TARGET_BUCKET: sorted(graph.files[stream.TARGET_BUCKET])}
        graph.walk(parsed, stream.TARGET_BUCKET, stream.ROOT + "/index.json")
        self.assertIn((stream.TARGET_BUCKET, document), graph.references)
        self.assertIn((stream.TARGET_BUCKET, original), graph.references)
        self.assertFalse(graph.missing)

    def test_utf8_only_apply_checkpoints_verification_without_publishing_a_derivative(self):
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
                 patch.object(stream,"publish_checkpoint", return_value={"files": []}) as publish, \
                 patch.dict(os.environ,{"HF_TOKEN":"test","GITHUB_OUTPUT":str(root/"outputs")}):
                self.assertEqual(stream.main(),0)
                publish.assert_called_once()
                self.assertEqual(publish.call_args.args[1:4], ([], [], {}))
                checked = publish.call_args.kwargs["unchanged_utf8"]
                self.assertEqual(len(checked), 1)
                self.assertEqual(checked[0]["key"], "VoiceOfML/Test\0readme.md")
                report=json.loads(args.report.read_text())
                self.assertEqual(report["published"],0)
                self.assertEqual(len(report["unchanged_utf8"]),1)

    def test_utf8_ledger_preserves_every_mapping_and_advances_the_next_small_batch(self):
        key = "VoiceOfML/Test\0readme.md"
        checked = {"key": key, "source_revision": "a" * 40, "source_sha256": "b" * 64,
                   "source_bytes": 17, "source_encoding": "utf-8"}
        previous = {"version": 1, "kind": "text-stream-index", "files": [{"key": key, "object": "old.txt"}]}
        stored, uploads = [copy.deepcopy(previous)], []
        def put(bucket, additions, token):
            for local, remote in additions:
                uploads.append(remote)
                self.assertEqual(remote, stream.ROOT + "/index.json")
                stored[0] = json.loads(Path(local).read_text())
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stream, "existing_index", side_effect=lambda *args: stored[0]), \
                patch.object(stream, "batch_bucket_files_with_retry", side_effect=put), \
                patch.object(stream, "read_bytes") as body_read:
            next_index = stream.publish_checkpoint(previous, [], [], {}, Path(directory), stream.TARGET_BUCKET, "test", unchanged_utf8=[checked])
        self.assertEqual(next_index["files"], previous["files"])
        self.assertEqual(uploads, [stream.ROOT + "/index.json"])
        body_read.assert_not_called()
        sources = [{"repo": "VoiceOfML/Test", "path": "readme.md", "revision": "a" * 40},
                   {"repo": "VoiceOfML/Test", "path": "next.md", "revision": "a" * 40}]
        self.assertEqual(stream.pending_records(sources, next_index), [sources[1]])
        self.assertEqual(stream.pending_records(sources, next_index, force=True), sources)
        self.assertEqual(stream.pending_records([{**sources[0], "revision": "c" * 40}], next_index), [{**sources[0], "revision": "c" * 40}])

    def test_malformed_utf8_ledger_cannot_suppress_sources(self):
        payload = {"version": 1, "kind": "text-stream-index", "files": [], "verified_utf8": [{"key": "missing-hash"}]}
        with patch.object(stream, "read_json", return_value=payload), self.assertRaises(ValueError):
            stream.existing_index(stream.TARGET_BUCKET, None)

    def test_two_limit_one_runs_reach_two_utf8_sources_without_touching_mappings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps([{"Repo": "VoiceOfML/Test", "File": name, "Extension": "md", "Folder": []}
                                          for name in ("first", "second")]))
            revisions = root / "revisions.json"
            revisions.write_text(json.dumps({"VoiceOfML/Test": "a" * 40}))
            args = SimpleNamespace(search_data=source, revisions=revisions, extensions=["md"], limit=1,
                checkpoint_size=64, bucket=stream.TARGET_BUCKET, apply=True, plan_only=False,
                force=False, retry_failures=False, encoding_overrides=None, report=root / "report.json")
            stored = [{"version": 1, "kind": "text-stream-index", "files": []}]
            seen = []
            def download(url, target, token):
                seen.append(url.rsplit("/", 1)[-1]); target.write_bytes(b"# correct UTF-8\n")
            def upload(bucket, additions, token):
                for local, remote in additions:
                    self.assertEqual(remote, stream.ROOT + "/index.json")
                    stored[0] = json.loads(Path(local).read_text())
            with patch.object(stream, "parse_args", return_value=args), \
                    patch.object(stream, "existing_index", side_effect=lambda *args: stored[0]), \
                    patch.object(stream, "download", side_effect=download), \
                    patch.object(stream, "batch_bucket_files_with_retry", side_effect=upload), \
                    patch.dict(os.environ, {"HF_TOKEN": "test"}):
                self.assertEqual(stream.main(), 0)
                self.assertEqual(stream.main(), 0)
                self.assertEqual(stream.main(), 0)
            self.assertEqual(seen, ["first.md", "second.md"])
            self.assertEqual(stored[0]["files"], [])
            self.assertEqual(len(stored[0]["verified_utf8"]), 2)

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
        workflow = Path(__file__).parents[1] / ".github/workflows/reader-publish-text.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("inputs.apply == true && 'reader-sidecar'", text)
        self.assertIn("reader-text-plan-{0}", text)
        self.assertIn("--plan-only", text)
        self.assertIn("pilot_text_stream.py", text)
        self.assertIn("TEXT_ENCODINGS: ${{ inputs.encoding_overrides_json || '{}' }}", text)
        self.assertIn("json.loads(os.environ['TEXT_ENCODINGS'])", text)
        self.assertIn("--encoding-overrides output/text-encoding-overrides.json", text)
        self.assertNotIn("run: ${{ inputs.encoding_overrides_json", text)
        self.assertIn("build_reader_assets_v2_sidecar.py --apply", text)
        self.assertNotIn("run: python scripts/publish_source_txt_once.py", text)
        self.assertTrue(producer_path(".github/workflows/reader-publish-text.yml"))


if __name__ == "__main__":
    unittest.main()
