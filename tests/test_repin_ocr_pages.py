import gzip
import hashlib
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import pdf_ocr_stages as stages
from scripts import repin_ocr_pages as repin

ROOT = "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr"


def payload(page, text="字", **extra):
    return gzip.compress(json.dumps({"kind": "pdf-ocr-page", "page": page, "source": "ocr", "width": 10,
                                     "height": 20, "text": text, "blocks": [], "text_spans": [],
                                     "layout": {}, **extra}).encode(), mtime=0)


def manifest(objects):
    return {"pages": [{"p": n, "source": "ocr", "width": 10, "height": 20,
                       "o": f"{ROOT}/page-{n:06d}.json.gz", "os": hashlib.sha256(raw).hexdigest(), "ob": len(raw)}
                      for n, raw in objects.items()]}


class AdoptPagesTests(unittest.TestCase):
    def test_adopts_current_bytes_and_counts_drifted_pages(self):
        old = {1: payload(1), 2: payload(2)}
        current = {f"{ROOT}/page-000001.json.gz": old[1], f"{ROOT}/page-000002.json.gz": payload(2, "新")}
        saved, adopted = repin.adopt_pages(manifest(old), current.__getitem__)
        self.assertEqual(adopted, 1)
        raw = current[f"{ROOT}/page-000002.json.gz"]
        self.assertEqual((saved["2"]["os"], saved["2"]["ob"], saved["2"]["text"]),
                         (hashlib.sha256(raw).hexdigest(), len(raw), "新"))
        self.assertEqual(saved["1"]["os"], hashlib.sha256(old[1]).hexdigest())

    def test_rejects_payload_of_another_page_or_size(self):
        old = {1: payload(1)}
        for wrong in (payload(2), payload(1, width=11)):
            with self.subTest(wrong=wrong[:4]):
                with self.assertRaisesRegex(ValueError, "not this page"):
                    repin.adopt_pages(manifest(old), lambda path, raw=wrong: raw)


class RepinRootTests(unittest.TestCase):
    def test_repin_assembles_into_a_distinct_root(self):
        roots = []
        original = stages.root_for
        def capture(*args):
            roots.append(args[2])
            return original(*args)
        book = {"render_manifest": {"sha256": "r" * 64}, "profile": "p", "source_sha256": "a" * 64, "key": "k"}
        class Empty(list):
            def close(self):
                pass
        with unittest.mock.patch.object(stages, "root_for", side_effect=capture), \
                unittest.mock.patch.object(stages.shared, "ordered_reads", return_value=Empty()), \
                unittest.mock.patch.object(stages.pdf_reading_v3, "build_text_bundle", side_effect=RuntimeError("stop")):
            for extra in ({}, {"repin": "c" * 16}):
                with self.assertRaises(RuntimeError):
                    stages.assemble_book({**book, **extra, "status": "ready", "pages": []}, {}, Path(tempfile.gettempdir()))
        self.assertEqual(len(roots), 2)
        self.assertNotEqual(roots[0], roots[1])
        self.assertTrue(roots[1].endswith("|repin-" + "c" * 16))


if __name__ == "__main__":
    unittest.main()
