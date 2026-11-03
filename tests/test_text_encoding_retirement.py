"""Text normalization stays usable after removing the legacy Space publisher."""
import codecs
from pathlib import Path
import subprocess
import sys
import unittest

from scripts.text_encoding import STRICT_ENCODINGS, looks_like_lfs_pointer, normalize_utf8, text_quality_score


ROOT = Path(__file__).resolve().parents[1]


class TextEncodingRetirementTests(unittest.TestCase):
    def test_score_and_lfs_detection_preserve_previous_behavior(self):
        self.assertEqual(STRICT_ENCODINGS, ("utf-8", "gb18030", "gbk", "big5", "cp932", "shift_jis", "euc_jp"))
        self.assertEqual(text_quality_score(""), -1_000_000)
        self.assertAlmostEqual(text_quality_score("中あ한A\ufffd\x01Я"), 12 + 0.15 - 30 - 20 - 6)
        self.assertTrue(looks_like_lfs_pointer(b"version https://git-lfs.github.com/spec/v1\noid sha256:x"))
        self.assertFalse(looks_like_lfs_pointer(b"text version https://git-lfs.github.com/spec/v1\n"))

    def test_legacy_text_and_direct_script_import_continue_to_work(self):
        raw = "中文文本和完整正文。".encode("gb18030")
        output, metadata = normalize_utf8(raw)
        self.assertEqual(output.decode("utf-8-sig"), raw.decode("gb18030"))
        self.assertEqual(metadata["source_encoding"], "gb18030")
        self.assertTrue(output.startswith(codecs.BOM_UTF8))
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import text_encoding; print(text_encoding.text_quality_score('中文'))"],
            cwd=ROOT / "scripts", capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "8.0")
        self.assertFalse((ROOT / "scripts/publish_source_txt_once.py").exists())


if __name__ == "__main__":
    unittest.main()
