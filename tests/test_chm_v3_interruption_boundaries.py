"""Synthetic interruption characterization: no network or production adapter.

Only a Python dict is written. These tests illustrate observed call ordering,
not S3/HF semantics, durable transactions, external fencing or recovery rights.
No real CHM, current catalog, processing pin or document receipt is touched.
"""

import ast
import copy
import gzip
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import chm_single_interrupted_preflight as fixed
from scripts import chm_single_v3_projection_shadow as model
from scripts import publish_reader_v3 as publication
from scripts import reader_document_reset as reset
from scripts import reader_gc_retirement as retirement


SIDECAR = 'reader-index/reader_assets.json.gz'


class InjectedTimeout(TimeoutError):
    pass


class SyntheticStore:
    def __init__(self, objects):
        self.objects = copy.deepcopy(objects)
        self.attempts = []
        self.fail_at = None
        self.fail_when = None

    def assert_serialized_writer(self, protocol):
        if protocol != publication.PROTOCOL:
            raise AssertionError('unexpected protocol')
        # This synthetic assertion does not fence any external writer.

    def read_bytes(self, bucket, path):
        try:
            return self.objects[bucket, path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def put_bytes(self, bucket, path, raw):
        self.attempts.append((bucket, path))
        fail = len(self.attempts) == self.fail_at
        if fail and self.fail_when == 'before':
            raise InjectedTimeout('synthetic timeout before write')
        self.objects[bucket, path] = bytes(raw)
        if fail and self.fail_when == 'after':
            raise InjectedTimeout('synthetic timeout after write')


class ChmV3InterruptionBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.old = {'v': 1, 'f': {'fixture\0book.chm': {'s': 2, 'm': 'e', 'p': 'chapters/old/manifest.json'},
                                   'fixture\0book.djvu': {'s': 2, 'm': 'p', 'p': 'documents/original.djvu'},
                                   'fixture\0audio.aac': {'s': 2, 'm': 'a', 'p': 'media/audio/keep'},
                                   'fixture\0native.txt': {'s': 2, 'm': 't', 'p': 'documents/text/keep'}}}
        self.new = copy.deepcopy(self.old)
        self.new['f']['fixture\0book.chm']['p'] = 'chapters/new/manifest.json'
        old_raw = gzip.compress(publication.encode(self.old), mtime=0)
        new_raw = gzip.compress(publication.encode(self.new), mtime=0)
        old_hash = publication.hashlib.sha256(old_raw).hexdigest()
        self.old_base = publication.metadata(publication.ASSETS,
            f'reader-index/snapshots/{old_hash}/reader_assets.json.gz', old_raw)
        catalog_body = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                        'base_sidecar': self.old_base, 'parent_generation': None,
                        'document_reset': model.RESET_ID}
        generation = publication.text.digest(catalog_body)
        self.old_catalog = {**catalog_body, 'generation': generation}
        catalog_raw = publication.encode(self.old_catalog)
        ref = publication.metadata(publication.ASSETS,
            f'reader-index/v3/generations/{generation}/catalog.json', catalog_raw)
        self.old_pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': generation,
                            'catalog': ref, 'resources': [ref]}
        self.receipt = {'version': 1, 'kind': 'reader-document-reset', 'status': 'withdrawn',
                        'reset_id': model.RESET_ID, 'generation': generation, 'base_snapshot': self.old_base}
        self.pin_paths = [f'reader-index/processing/fixture-{index:02}.json' for index in range(52)]
        objects = {(publication.ASSETS, SIDECAR): new_raw,
                   (publication.ASSETS, publication.POINTER): publication.encode(self.old_pointer),
                   (publication.ASSETS, ref['path']): catalog_raw,
                   (publication.ASSETS, self.old_base['path']): old_raw,
                   (reset.ASSETS, reset.RECEIPT): publication.encode(self.receipt)}
        objects.update({(publication.ASSETS, path): f'pin-{index}'.encode()
                        for index, path in enumerate(self.pin_paths)})
        self.initial = objects

    def run_with_writes(self, store):
        with patch.object(retirement, 'record', return_value=None) as hint:
            result = publication.unify(store, apply=True)
        hint.assert_called_once()
        self.assertEqual(hint.call_args.args[:2], ([], []))
        return result

    def test_timeout_before_and_after_every_v3_write_preserves_different_partial_states(self):
        success = SyntheticStore(self.initial)
        result = self.run_with_writes(success)
        self.assertEqual(len(success.attempts), 4)
        paths = [path for _bucket, path in success.attempts]
        self.assertTrue(paths[0].startswith('reader-index/snapshots/'))
        self.assertTrue(paths[1].startswith('reader-index/v3/generations/'))
        self.assertTrue(paths[2].startswith('reader-index/v3/promotions/'))
        self.assertEqual(paths[3], publication.POINTER)
        self.assertNotEqual(result['generation'], self.old_pointer['generation'])

        for position in range(1, len(paths) + 1):
            for timing in ('before', 'after'):
                with self.subTest(position=position, timing=timing):
                    store = SyntheticStore(self.initial)
                    store.fail_at, store.fail_when = position, timing
                    with patch.object(retirement, 'record', return_value=None), self.assertRaises(InjectedTimeout):
                        publication.unify(store, apply=True)
                    completed = position - 1 + (timing == 'after')
                    changed = {path for (bucket, path), raw in store.objects.items()
                               if bucket == publication.ASSETS and self.initial.get((bucket, path)) != raw}
                    self.assertEqual(changed, set(paths[:completed]))
                    pointer, catalog = publication.current(store)
                    pointer_committed = position == 4 and timing == 'after'
                    self.assertEqual(pointer['generation'], result['generation'] if pointer_committed
                                     else self.old_pointer['generation'])
                    if pointer_committed:
                        # Current unify() does not carry document_reset into the
                        # new candidate; the old receipt remains bound to parent.
                        self.assertNotIn('document_reset', catalog)
                        self.assertNotEqual(catalog['base_sidecar'], self.receipt['base_snapshot'])
                    else:
                        self.assertEqual(catalog, self.old_catalog)
                    self.assertEqual(store.read_bytes(reset.ASSETS, reset.RECEIPT), publication.encode(self.receipt))
                    self.assertEqual([store.read_bytes(publication.ASSETS, path) for path in self.pin_paths],
                                     [f'pin-{index}'.encode() for index in range(52)])
                    self.assertEqual(len(store.attempts), position)
                    # In particular, timeout after pointer write is ambiguous to
                    # the caller: no test here repeats, fixes or authorizes it.

    def test_all_synthetic_v3_paths_leave_canonical_sidecar_receipt_and_pins_untouched(self):
        store = SyntheticStore(self.initial)
        self.run_with_writes(store)
        self.assertEqual(store.read_bytes(publication.ASSETS, SIDECAR), self.initial[publication.ASSETS, SIDECAR])
        self.assertEqual(store.read_bytes(reset.ASSETS, reset.RECEIPT), self.initial[reset.ASSETS, reset.RECEIPT])
        for path in self.pin_paths:
            self.assertEqual(store.read_bytes(publication.ASSETS, path), self.initial[publication.ASSETS, path])
        self.assertFalse(any('/gc/retirements/' in path for _bucket, path in store.attempts))

    def test_no_real_constants_storage_or_workflow_activation_in_test(self):
        self.assertNotIn(fixed.SOURCE_KEY, self.old['f'])
        tree = ast.parse(Path(__file__).read_text())
        self.assertFalse([node for node in ast.walk(tree) if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name) and node.func.id in ('S3BucketStore', 'HubBucketStore')])
        workflow_root = Path(__file__).resolve().parents[1] / '.github/workflows'
        self.assertFalse([path for path in workflow_root.glob('*.yml')
                          if 'test_chm_v3_interruption_boundaries' in path.read_text()])


if __name__ == '__main__':
    unittest.main()
