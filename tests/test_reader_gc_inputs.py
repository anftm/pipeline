import json
import unittest

from scripts import reader_gc_inputs as inputs

SOURCE = "a" * 64
ROOT = f"objects/aa/{SOURCE}/{'b' * 16}/ocr-input"


class Store:
    def __init__(self):
        self.objects, self.object_sizes, self.deleted = {}, {}, []

    def put(self, bucket, path, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.objects[(bucket, path)] = raw
        self.object_sizes[(bucket, path)] = len(raw)

    def read_bytes(self, bucket, path):
        if (bucket, path) not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[(bucket, path)]

    def list_files(self, bucket, prefixes):
        return {p for b, p in self.objects if b == bucket and p.startswith(prefixes)}

    def delete(self, bucket, paths):
        for path in paths:
            self.objects.pop((bucket, path), None)
        self.deleted.extend(paths)


class RetiredInputTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        for n in (1, 2):
            self.store.put(inputs.INPUT, f"{ROOT}/page-00000{n}.png", b"png")
        self.store.put(inputs.INPUT, f"{ROOT}/notes.json", b"{}")
        self.store.put(inputs.INPUT, f"objects/aa/{SOURCE}/{'b' * 16}/pages/page-000001.jxl", b"jxl")
        self.store.put(inputs.ASSETS, inputs.JOURNAL, {"version": 1, "kind": "reader-v3-retirement",
            "retired_input_roots": {ROOT: {"bucket": inputs.INPUT, "root": ROOT, "source_sha256": SOURCE,
                                           "ocr_manifest_sha256": "c" * 64, "pages": 2}}})
        self.registry(status="ready", digest="c" * 64)

    def registry(self, *, status, digest):
        self.store.put(inputs.ASSETS, inputs.REGISTRY, {"files": {"repo\0book.pdf": {
            "status": status, "source_sha256": SOURCE, "ocr_manifest_sha256": digest}}})

    def test_deletes_only_page_pngs_of_released_root(self):
        report = inputs.plan(self.store)
        self.assertEqual(report["planned_objects"], 2)
        inputs.collect(self.store, report)
        self.assertEqual(sorted(self.store.deleted), [f"{ROOT}/page-000001.png", f"{ROOT}/page-000002.png"])
        self.assertIn((inputs.INPUT, f"{ROOT}/notes.json"), self.store.objects)
        self.assertEqual(report["deleted"]["objects"], 2)

    def test_changed_ocr_registry_keeps_inputs(self):
        self.registry(status="ready", digest="d" * 64)
        report = inputs.plan(self.store)
        self.assertEqual(report["planned_objects"], 0)
        self.assertEqual(report["skipped"], {"ocr-registry-changed": 1})

    def test_ccrd_retained_input_is_kept(self):
        self.store.put(inputs.CCRD, "control/ocr-current.json", {"release": {"path": "releases/r/manifest.json"}})
        self.store.put(inputs.CCRD, "releases/r/manifest.json", {"exports": [{"path": "text/exports/e.json"}]})
        self.store.put(inputs.CCRD, "text/exports/e.json", {"retained_reader_resources": [
            {"bucket": inputs.INPUT, "path": f"{ROOT}/page-000001.png"}]})
        report = inputs.plan(self.store)
        self.assertEqual(report["planned_objects"], 0)
        self.assertEqual(report["skipped"], {"ccrd-retained": 1})


if __name__ == "__main__":
    unittest.main()
