"""Synthetic one-hop reuse tests; never change real GC or publish resources."""
from copy import deepcopy
import gzip
import json
import sqlite3
import unittest
from unittest.mock import patch

from scripts import ccrd_lifecycle as lifecycle
from scripts import ccrd_single_source_reuse_shadow as reuse
from scripts import reader_gc_graph as graph
from tests import test_ccrd_single_source_manifest_draft as fixture


class MemoryStore:
    """Minimal read-only GC fixture; no production dependencies or deletion."""
    def __init__(self):
        self.objects = {bucket: {} for bucket in graph.BUCKETS}
        self.objects[graph.READER_BUCKETS[0]]['reader-index/reader_assets.json.gz'] = gzip.compress(
            json.dumps({'v': 1, 'f': {}}).encode(), mtime=0)

    def read_bytes(self, bucket, path):
        return self.objects[bucket][path]

    def list_files(self, bucket, prefixes):
        return {path for path in self.objects[bucket] if path.startswith(prefixes)}


@unittest.skipUnless(hasattr(sqlite3.Connection, 'serialize') and hasattr(sqlite3.Connection, 'deserialize'),
                     'SQLite byte serialization requires a supported Python build')
class ReuseShadowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ManifestDraftTests('test_parent_summary_is_reference_only')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.parent = self.fixture.snapshot.pointer_bytes
        self.base_draft = self.fixture.make()
        self.result = reuse.propose(self.parent, self.base_draft)
        self.child = self.result['manifest']

    def test_exact_parent_archive_and_unchanged_metadata_are_reused(self):
        original = self.fixture.parent
        self.assertEqual(sorted(self.child['archives'], key=lambda r: r['path']),
                         sorted(original['archives'], key=lambda r: r['path']))
        self.assertEqual(self.result['archive_paths_reused'], len(original['archives']))
        old = {ref['sha256']: ref for ref in original['metadata']}
        unchanged = [ref for ref in self.child['metadata'] if ref['sha256'] in old and ref == old[ref['sha256']]]
        self.assertEqual(self.result['metadata_paths_reused'], len(unchanged))
        self.assertGreater(len(unchanged), 0)
        self.assertNotEqual(self.child['generation'], self.base_draft['manifest']['generation'])
        self.assertEqual(self.child['databases']['CW']['path'],
                         f'generations/{self.child["generation"]}/CW.sqlite3')
        for source in ('CCRD', 'RMRB', 'PLA'):
            self.assertEqual(self.child['databases'][source], original['databases'][source])
        self.assertFalse(self.result['publication_ready'])
        self.assertFalse(self.result['actual_objects_verified'])
        self.assertFalse(self.result['gc_safety_proven'])
        self.assertEqual(self.result['remote_writes'], 0)
        self.assertEqual(self.result, reuse.propose(self.parent, self.base_draft))

    def test_existing_lifecycle_rejects_reuse_and_retains_its_original_contract(self):
        with self.assertRaisesRegex(ValueError, 'outside its generation'):
            lifecycle.references(self.child, 'current.json')
        lifecycle.references(self.base_draft['manifest'], 'current.json')
        paths = {(ref['bucket'], ref['path']) for ref in reuse.references(self.child, self.parent)}
        for ref in self.child['archives']:
            self.assertIn((ref['bucket'], ref['path']), paths)
        self.assertFalse(self.result['existing_lifecycle_compatible'])

    def test_missing_modified_or_redirected_parent_resources_fail(self):
        for mode in ('missing-archive', 'modified-archive-hash', 'redirected-archive', 'metadata-digest',
                     'wrong-parent', 'other-index', 'cw-path', 'unknown-metadata', 'duplicate-metadata'):
            child = deepcopy(self.child)
            parent = self.parent
            if mode == 'missing-archive':
                child['archives'].pop()
            elif mode == 'modified-archive-hash':
                child['archives'][0]['sha256'] = 'f' * 64
            elif mode == 'redirected-archive':
                child['archives'][0]['path'] = child['archives'][0]['path'].replace('a' * 64, 'b' * 64)
            elif mode == 'metadata-digest':
                selected = next(ref for ref in child['metadata'] if ref['path'].startswith('generations/ccrd-four-' + 'a' * 64))
                selected['sha256'] = 'f' * 64
            elif mode == 'wrong-parent':
                parent += b' '
            elif mode == 'other-index':
                child['databases']['PLA']['sha256'] = 'f' * 64
            elif mode == 'cw-path':
                child['databases']['CW']['path'] = 'generations/ccrd-other/CW.sqlite3'
            elif mode == 'unknown-metadata':
                child['metadata'][0]['path'] = f'generations/{child["generation"]}/data/unreviewed.json'
            else:
                child['metadata'].append(deepcopy(child['metadata'][0]))
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                reuse.references(child, parent)

    def test_candidate_from_unrelated_parent_and_unknown_profile_are_rejected(self):
        for mode in ('parent', 'archive', 'database', 'published'):
            candidate = deepcopy(self.base_draft)
            if mode == 'parent':
                candidate['manifest']['previous_index_manifest_sha256'] = 'f' * 64
            elif mode == 'archive':
                candidate['manifest']['archives'][0]['sha256'] = 'f' * 64
            elif mode == 'database':
                candidate['manifest']['databases']['PLA']['sha256'] = 'f' * 64
            else:
                candidate['publication_ready'] = True
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                reuse.propose(self.parent, candidate)

    def test_shadow_cannot_claim_recursive_reuse_or_remote_effects(self):
        child_raw = self.result['manifest_bytes']
        with self.assertRaises(ValueError):
            reuse.propose(child_raw, self.base_draft)
        with patch.object(lifecycle, 'references', wraps=lifecycle.references) as original:
            refs = reuse.references(self.child, self.parent)
            self.assertTrue(original.called)
        self.assertEqual(len(refs), 4 + len(self.child['metadata']) + len(self.child['archives']))
        self.assertTrue(self.result['no_archive_copy_is_only_a_model'])

    def test_gc_graph_requires_compatible_validator_and_retains_all_old_archives(self):
        store = MemoryStore()
        store.objects[lifecycle.BUCKET]['current.json'] = self.result['manifest_bytes']
        for ref in self.result['references']:
            # Ordinary archive/data references are marked by path; their
            # fabricated content here is not a full hash verification.
            store.objects[ref['bucket']][ref['path']] = b'synthetic presence'
        existing = graph.ReferenceGraph(store)
        existing_report = existing.build()
        self.assertFalse(existing_report['graph_complete'])
        original = lifecycle.references

        def candidate_only(value, path):
            if path == 'current.json' and value == self.child:
                return reuse.references(value, self.parent)
            return original(value, path)

        with patch.object(lifecycle, 'references', side_effect=candidate_only):
            updated = graph.ReferenceGraph(store)
            report = updated.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        for ref in self.child['archives']:
            self.assertIn((ref['bucket'], ref['path']), updated.references)
            self.assertNotIn(ref['path'], report['buckets'][ref['bucket']]['candidates'])
        self.assertFalse(report['deletion_enabled'])
        self.assertFalse(self.result['gc_safety_proven'])


if __name__ == '__main__':
    unittest.main()
