import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts import ocr_layout, ocr_quality as quality, pdf_ocr, pdf_ocr_stages as stages


def block(text, box):
    return {"t": text, "b": box, "c": .999, "s": "ocr"}


def page(blocks, language="ch", options=None):
    evidence = {"source": "page-image", "scripts": ["Han"], "probe": {"blank": False}}
    payload = {**pdf_ocr.page_payload(1, 1200, 1800, blocks, "ocr"),
               **ocr_layout.arrange(blocks, 1200, 1800, options),
               "recognition": {"policy": quality.POLICY, "language": language,
                   "language_evidence": evidence, "source_sha256": "a" * 64, "input_sha256": "b" * 64,
                   "profile": "fixture", "models": {"models": {"det": {"sha256": "c" * 64},
                                                               "rec": {"sha256": "d" * 64}}}}}
    return payload


def codes(report):
    return {i["code"] for i in report["issues"]}


def reorder(payload, indices):
    """Simulate valid offsets with the old, semantically wrong reading order."""
    raw = payload["raw_blocks"]
    payload["text"], payload["text_spans"] = "", []
    for i in indices:
        if payload["text"]:
            payload["text"] += "\n"
        start = len(payload["text"])
        payload["text"] += raw[i]["t"]
        payload["text_spans"].append({"start": start, "end": len(payload["text"]), "block": i, "box": raw[i]["b"]})


class QualityTests(unittest.TestCase):
    def test_native_assembly_keeps_original_evidence_indices(self):
        blocks = [block('second', [.1, .4, .4, .45]), block('first', [.1, .1, .4, .15])]
        payload = {**pdf_ocr.page_payload(1, 1000, 1500, blocks, 'native'),
                   **ocr_layout.arrange(blocks, 1000, 1500)}
        source = {'p': 1, 'source': 'native', 'o': 'objects/ocr/page-000001.json.gz',
                  'os': 'b' * 64, 'ob': 123}
        stages.assembled_page({'source_sha256': 'a' * 64}, source, payload, 'en')
        self.assertEqual(payload['raw_blocks'], blocks)
        self.assertEqual([span['block'] for span in payload['text_spans']], [1, 0])
        self.assertEqual(quality.check_page(payload)['status'], 'passed')

    def test_sparse_punctuation_is_not_silently_passed_when_auditor_finds_it(self):
        payload = page([block("日本語の本文です" * 10, [.1, .1, .9, .2])], "japan")
        payload["recognition"]["models"]["diagnostics"] = {"secondary_check": {
            "status": "review", "issues": [quality.issue("recognizers-disagree-on-punctuation", "review",
                primary=0, secondary=12)], "models": {"rec": {"sha256": "e" * 64}}}}
        self.assertIn("recognizers-disagree-on-punctuation", codes(quality.check_page(payload)))

    def test_high_confidence_wrong_language_and_override_are_rejected(self):
        payload = page([block("Русский текст " * 30, [.1, .1, .9, .2])])
        payload["recognition"]["language_evidence"]["source"] = "override"
        report = quality.check_page(payload)
        self.assertEqual(report["status"], "failed")
        self.assertIn("recognition-language-script-mismatch", codes(report))

    def test_regional_cyrillic_evidence_preserves_bilingual_page(self):
        probe = {"regions": [{"script": "Latin", "confidence": .68},
                              {"script": "Latin", "confidence": 2.41},
                              {"script": "Cyrillic", "confidence": .48}]}
        self.assertEqual(quality.select_language("ch", "filename", probe)["language"], "ru")
        explicit = quality.select_language("ch", "override", probe)
        self.assertEqual(explicit["language"], "ch")
        self.assertIn("explicit-language-conflicts-with-image", codes(explicit))

    def test_french_archive_page_requires_image_and_loaded_model_coverage(self):
        probe = {'regions': [{'script': 'Latin', 'confidence': 5}]}
        evidence = quality.select_language('ru', 'override', probe)
        self.assertEqual(evidence['language'], 'ru')
        self.assertEqual(evidence['issues'], [])
        payload = page([block('Ministre de France, gouvernement français. ' * 5, [.1, .1, .9, .2])], 'ru')
        payload['recognition']['language_evidence'] = evidence
        self.assertIn('recognition-language-script-mismatch', codes(quality.check_page(payload)))
        payload['recognition']['models']['models']['rec']['language'] = 'cyrillic'
        self.assertEqual(quality.check_page(payload)['status'], 'passed')
        payload['recognition']['language_evidence']['scripts'] = ['Cyrillic']
        self.assertIn('recognition-language-script-mismatch', codes(quality.check_page(payload)))

    def test_routing_and_crop_policy_changes_invalidate_recognition_reuse(self):
        entry = {'key': 'k', 'ocr_language': 'ch'}
        current = stages.recognition_identity(entry, {})
        with patch.object(stages.ocr_script_evidence, 'PROFILE', 'different-crops'):
            self.assertFalse(stages.reusable_profile(current, stages.recognition_identity(entry, {})))
        with patch.object(quality, 'ROUTING_PROFILE', 'different-routing'):
            self.assertFalse(stages.reusable_profile(current, stages.recognition_identity(entry, {})))

    def test_three_consistent_low_confidence_votes_are_recorded(self):
        probe = {"regions": [{"script": "Han", "confidence": n} for n in (.81, .98, 1.03)]}
        result = quality.select_language("en", "filename", probe)
        self.assertEqual(result["language"], "ch")
        self.assertEqual(result["scripts"], ["Han"])

    def test_realistic_vertical_punctuation_disagreement_requires_review(self):
        rows = [{"region": i, "primary": "本当に近しい同志であたのだろう父の本を読んでいると",
                 "secondary": "本当に近しい同志であったのだろう。父の本を読んでいると、"} for i in range(3)]
        self.assertIn("recognizers-disagree-on-punctuation", codes(quality.compare_recognizers(rows, {})))

    def test_consistent_japanese_image_votes_outrank_chinese_filename(self):
        probe = {"regions": [{"script": "Japanese", "confidence": n} for n in (1.44, 1.78, 1.37)]}
        self.assertEqual(quality.select_language("ch", "filename", probe)["language"], "japan")

    def test_vertical_interleaving_fails_even_with_valid_offsets(self):
        blocks = [block("上段右列", [.75, .05, .8, .42]), block("上段左列", [.2, .05, .25, .42]),
                  block("下段右列", [.75, .55, .8, .93]), block("下段左列", [.2, .55, .25, .93])]
        payload = page(blocks)
        self.assertEqual(quality.check_page(payload)["status"], "passed")
        reorder(payload, [0, 2, 1, 3])
        self.assertIn("vertical-tiers-interleaved", codes(quality.check_page(payload)))

    def test_horizontal_columns_cannot_be_interleaved(self):
        blocks = [block("Column text", [left, top, left + .3, top + .04])
                  for left in (.1, .6) for top in (.1, .3, .5)]
        payload = page(blocks, "en")
        self.assertEqual(quality.check_page(payload)["status"], "passed")
        reorder(payload, [0, 3, 1, 4, 2, 5])
        self.assertIn("text-columns-interleaved", codes(quality.check_page(payload)))

    def test_dropped_text_and_bad_search_coordinates_fail(self):
        payload = page([block("第一行", [.1, .1, .8, .2]), block("第二行", [.1, .3, .8, .4])])
        payload["text_spans"].pop()
        self.assertIn("text-blocks-dropped-or-duplicated", codes(quality.check_page(payload)))
        payload = page([block("第一行", [.1, .1, .8, .2])])
        payload["text_spans"][0]["box"] = [.1, .6, .8, .8]
        self.assertIn("text-span-or-coordinate-mismatch", codes(quality.check_page(payload)))

    def test_blank_needs_independent_image_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "page.png"
            image = Image.new("L", (200, 300), 255)
            image.save(path)
            with patch.object(quality.subprocess, "run", side_effect=AssertionError("blank needs no OSD")):
                probe = quality.probe_image(path)
            self.assertTrue(probe["blank"])
            payload = page([])
            self.assertIn("empty-recognition-on-unverified-page", codes(quality.check_page(payload)))
            payload["recognition"]["language_evidence"]["probe"] = probe
            self.assertEqual(quality.check_page(payload)["status"], "passed")
            ImageDraw.Draw(image).rectangle((40, 50, 80, 70), fill=0)
            image.save(path)
            with patch.object(quality.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="Too few characters")):
                self.assertFalse(quality.probe_image(path)["blank"])

    def test_absent_model_digest_is_review_not_passed(self):
        payload = page([block("正文", [.1, .1, .8, .2])])
        payload["recognition"]["models"] = {"models": {"det": {"size": "small"}, "rec": {"size": "small"}}}
        self.assertEqual(quality.check_page(payload)["status"], "review")

    def test_model_or_layout_change_cannot_reuse_same_png(self):
        base = {"key": "k", "source_sha256": "a" * 64, "ocr_language": "ch"}
        old_profile = stages.recognition_identity(base, {})
        new_profile = stages.recognition_identity({**base, "ocr_language": "ru"}, {})
        self.assertFalse(stages.reusable_profile(old_profile, new_profile))
        self.assertFalse(stages.reusable_profile(old_profile, old_profile.replace("layout-" + ocr_layout.VERSION, "layout-v1")))
        previous = {"source_sha256": base["source_sha256"], "profile": old_profile, "pages": {"1": {"p": 1}}}
        self.assertEqual(stages.reuse_progress_pages(previous, {**base, "profile": new_profile}, []), {})

    def test_book_cannot_pass_when_page_missing_or_review_required(self):
        first = quality.check_page(page([block("正文", [.1, .1, .8, .2])]))
        self.assertEqual(quality.book_report([first], 2)["status"], "failed")
        second = {**first, "page": 2, "issues": [quality.issue("unresolved", "review")]}
        self.assertEqual(quality.book_report([first, second], 2)["affected_pages"], [2])

    def test_checkpoint_keeps_errors_until_page_successfully_retries(self):
        old = {"generation": "g", "pages": {}, "errors": [{"page": 2, "code": "failed"}]}
        partial = stages.merge_progress(old, {"generation": "g", "pages": {"1": {"p": 1}}})
        self.assertEqual(partial["errors"], old["errors"])
        resumed = stages.merge_progress(partial, {"generation": "g", "pages": {"2": {"p": 2}}})
        self.assertEqual(resumed["errors"], [])

    def test_detector_only_output_cannot_become_blank_page(self):
        with self.assertRaisesRegex(RuntimeError, "lengths differ"):
            pdf_ocr.normalize_rapid_result({"boxes": [[[0, 0], [1, 0], [1, 1], [0, 1]]]}, 10, 10)

    def test_quality_report_is_written_before_non_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path, summary = Path(temp) / "report.json", Path(temp) / "summary.md"
            report = {"results": [{"key": "repo\0book.pdf", "errors": [{"page": 4, "error": "bad input"}]}]}
            with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}):
                stages.write_execution_report(report, path)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertIn("failed or require review", summary.read_text())

    def test_rejected_ocr_removes_old_text_reference_and_preserves_reading_images(self):
        from scripts import shared
        entry = {"s": 1, "p": "objects/image/page-manifest.json", "b": shared.PDF_PAGES_BUCKET,
                 "o": "objects/old/ocr-manifest.json"}
        result = shared.merge_pdf_ocr_sidecar_entry(entry, {"status": "failed", "quality_status": "review", "error": "bad language"})
        self.assertEqual(result["p"], entry["p"])
        self.assertNotIn("o", result)
        self.assertEqual(result["om"], "needs-review")


if __name__ == "__main__":
    unittest.main()
