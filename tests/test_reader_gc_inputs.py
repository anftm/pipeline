import json
import unittest
import hashlib
from unittest.mock import patch

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

    def test_recently_released_root_is_kept_for_the_grace_period(self):
        journal = self.store.objects[(inputs.ASSETS, inputs.JOURNAL)]
        payload = json.loads(journal)
        payload["retired_input_roots"][ROOT]["released_at"] = (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        self.store.put(inputs.ASSETS, inputs.JOURNAL, payload)
        report = inputs.plan(self.store)
        self.assertEqual(report["planned_objects"], 0)
        self.assertEqual(report["skipped"], {"recently-released": 1})

    def test_old_release_is_collected(self):
        journal = self.store.objects[(inputs.ASSETS, inputs.JOURNAL)]
        payload = json.loads(journal)
        payload["retired_input_roots"][ROOT]["released_at"] = "2020-01-01T00:00:00+00:00"
        self.store.put(inputs.ASSETS, inputs.JOURNAL, payload)
        report = inputs.plan(self.store)
        self.assertEqual(report["planned_objects"], 2)

    def test_ccrd_retained_input_is_kept(self):
        self.pin_input()
        report = inputs.plan(self.store)
        self.assertEqual(report["planned_objects"], 0)
        self.assertEqual(report["skipped"], {"ccrd-retained": 1})

    def pin_input(self):
        ref = {'bucket': inputs.INPUT, 'path': f'{ROOT}/page-000001.png',
               'bytes': 3, 'sha256': hashlib.sha256(b'png').hexdigest(), 'role': 'review'}
        self.store.put(inputs.CCRD, 'control/intents/' + 'd' * 64 + '.json', {
            'version': 1, 'kind': 'ccrd-ocr-intent', 'status': 'uploading', 'generation': 'd' * 64,
            'expected_parent': None, 'resources': [ref]})

    def test_registry_change_after_plan_blocks_the_first_delete(self):
        report = inputs.plan(self.store)
        self.registry(status='ready', digest='d' * 64)
        with self.assertRaisesRegex(RuntimeError, 'registry changed'):
            inputs.collect(self.store, report)
        self.assertEqual(self.store.deleted, [])

    def test_new_upload_pin_after_plan_blocks_deletion(self):
        report = inputs.plan(self.store)
        self.pin_input()
        with self.assertRaisesRegex(RuntimeError, 'CCRD retained'):
            inputs.collect(self.store, report)
        self.assertEqual(self.store.deleted, [])

    def test_partial_delete_failure_keeps_evidence_and_next_plan_completes(self):
        report = inputs.plan(self.store)
        delete = self.store.delete
        def partial(bucket, paths):
            delete(bucket, paths[:1])
            raise OSError('lost acknowledgement')
        checkpoints = []
        with patch.object(self.store, 'delete', side_effect=partial), self.assertRaises(OSError):
            inputs.collect(self.store, report, checkpoint=lambda r: checkpoints.append(json.loads(json.dumps(r))))
        self.assertEqual(checkpoints[-1]['roots'][0]['status'], 'submitted')
        self.assertEqual(checkpoints[-1]['deleted']['objects'], 0)
        self.assertFalse(checkpoints[-1]['complete'])
        retry = inputs.plan(self.store)
        self.assertEqual(retry['planned_objects'], 1)
        inputs.collect(self.store, retry)
        self.assertEqual(retry['deleted']['objects'], 1)

    def test_successful_delete_receipt_cannot_hide_retained_sibling_loss(self):
        report = inputs.plan(self.store)
        delete = self.store.delete
        def remove(bucket, paths):
            delete(bucket, [*paths, ROOT + '/notes.json'])
        with patch.object(self.store, 'delete', side_effect=remove), self.assertRaisesRegex(RuntimeError, 'sibling'):
            inputs.collect(self.store, report)
        self.assertEqual(report['deleted']['objects'], 0)
        self.assertEqual(report['roots'][0]['batches'][0]['status'], 'accepted')

    def test_pin_added_during_delete_prevents_verified_success(self):
        report = inputs.plan(self.store)
        delete = self.store.delete
        def race(bucket, paths):
            delete(bucket, paths)
            self.pin_input()
        with patch.object(self.store, 'delete', side_effect=race), self.assertRaisesRegex(RuntimeError, 'CCRD retained'):
            inputs.collect(self.store, report)
        self.assertFalse(report['complete'])
        self.assertEqual(report['deleted']['objects'], 0)


if __name__ == "__main__":
    unittest.main()
