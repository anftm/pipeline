"""Synthetic four-source manifest drafts; never copy objects or publish."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import unittest

from scripts import ccrd_lifecycle as lifecycle
from scripts import ccrd_source_override as source_override
from scripts import ccrd_single_source_manifest_draft as draft
from scripts import ccrd_single_source_offline_chain as chain
from scripts import ccrd_single_source_plan as planner
from tests import test_ccrd_single_source_offline_chain as fixture
from tests import test_ccrd_single_source_plan as plan_fixture


@unittest.skipUnless(hasattr(sqlite3.Connection, 'serialize') and hasattr(sqlite3.Connection, 'deserialize'),
                     'SQLite byte serialization requires a supported Python build')
class ManifestDraftTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OfflineChainTests('test_no_change_skips_candidate_and_cache_generation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.args = deepcopy(self.fixture.args)
        old = json.loads(self.args['pointer_bytes'])
        old['generation'] = 'ccrd-four-' + 'a' * 64
        old['previous_base_metadata_sha256'] = '1' * 64
        old['previous_index_manifest_sha256'] = '2' * 64
        old['source_archive'] = {'path': 'fixture original source'}
        old['corpus_migration'] = {'kind': 'fixture historical migration'}
        old['source_overrides'][planner.TARGET_PATH].update(bytes=len(self.args['current_txt']), url='fixture')
        prefix = f'generations/{old["generation"]}/'
        for name, item in old['databases'].items():
            if name == 'CW':
                item['path'] = 'generations/ccrd-old/CW.sqlite3'
                db = self.fixture.index_fixture.open_bytes(self.args['database_bytes'])
                item['postings'] = db.execute('SELECT COUNT(*) FROM postings').fetchone()[0]
                item['snippet_anchors'] = db.execute('SELECT COUNT(*) FROM snippet_anchors').fetchone()[0]
            else:
                raw = f'unchanged {name} fixture'.encode()
                item.update(path=f'generations/ccrd-old/{name}.sqlite3', sha256=planner.sha(raw), bytes=len(raw))
                if name in {'RMRB', 'PLA'}:
                    item.update(path=f'indexes/newspapers/{name}/{item["sha256"]}.sqlite3', source=name,
                                index_profile='newspaper-hybrid-texts-v1', text_rows=item['documents'])

        self.metadata = {name: b'fixture-' + name.encode() for name in draft.METADATA_PATHS}
        self.metadata.update(self.fixture.payloads)
        self.metadata[draft.BASE_METADATA] = self.args['metadata_gz']
        old['metadata'] = [{'bucket': lifecycle.BUCKET, 'path': prefix + name, 'role': 'runtime',
                            'bytes': len(raw), 'sha256': planner.sha(raw)} for name, raw in sorted(self.metadata.items())]
        old['archives'] = []
        for name in ('RMRB', 'PLA'):
            raw = f'fixture original archive {name}'.encode()
            old['archives'].append({'bucket': lifecycle.BUCKET, 'path': prefix + f'archives/{name}/{name}-1976.tar.gz',
                                    'role': 'runtime', 'bytes': len(raw), 'sha256': planner.sha(raw),
                                    'source': name, 'year': 1976, 'documents': old['databases'][name]['documents']})
        self.parent = old
        self.set_parent(old)

    def set_parent(self, parent):
        self.args['pointer_bytes'] = plan_fixture.encode(parent)
        activation = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': lifecycle.BUCKET,
                      'path': f'generations/{parent["generation"]}/manifest.json',
                      'sha256': planner.sha(self.args['pointer_bytes'])}
        self.args['activation_bytes'] = plan_fixture.encode(activation)
        self.snapshot = chain.capture(**self.args, specifications=self.fixture.specs, payloads=self.fixture.payloads)
        point = None
        for _ in range(4):
            point = chain.step(self.snapshot, point)
        self.point = point
        self.remaining = {name: raw for name, raw in self.metadata.items()
                          if name not in self.fixture.payloads and name != draft.BASE_METADATA}

    def make(self):
        return draft.build(self.snapshot, self.point, remaining_metadata=self.remaining,
                           next_source_url=source_override.source_urls()[1])

    def test_four_source_manifest_binds_single_snapshot_and_preserves_parent(self):
        parent_before = deepcopy(self.parent)
        result = self.make()
        manifest = result['manifest']
        self.assertEqual(self.parent, parent_before)
        lifecycle.references(manifest, result['activation']['path'])
        self.assertEqual(result['manifest_bytes'], lifecycle.canonical(manifest))
        self.assertEqual(result['activation']['sha256'], planner.sha(result['manifest_bytes']))
        self.assertEqual(result['generation'], 'ccrd-four-' + planner.sha(lifecycle.canonical(result['generation_identity'])))
        self.assertEqual(manifest['previous_index_manifest_sha256'], planner.sha(self.args['pointer_bytes']))
        self.assertEqual(manifest['previous_base_metadata_sha256'], self.parent['base_metadata_sha256'])
        for name in ('CCRD', 'RMRB', 'PLA'):
            self.assertEqual(manifest['databases'][name], self.parent['databases'][name])
        self.assertEqual(manifest['source_overrides'][planner.TARGET_PATH]['sha256'], planner.sha(self.args['next_txt']))
        self.assertEqual(manifest['source_overrides'][planner.TARGET_PATH]['bytes'], len(self.args['next_txt']))
        self.assertEqual(manifest['source_overrides'][planner.TARGET_PATH]['url'], source_override.source_urls()[1])
        self.assertEqual(len(manifest['metadata']), len(draft.METADATA_PATHS))
        self.assertEqual(len(manifest['archives']), 2)
        self.assertEqual([ref['sha256'] for ref in manifest['archives']],
                         [ref['sha256'] for ref in sorted(self.parent['archives'], key=lambda ref: ref['path'])])
        self.assertEqual(set(result['space_changed_paths']), {planner.TARGET_PATH, 'data/index-release.json',
                         draft.BASE_METADATA, self.fixture.cache_fixture.initial,
                         self.fixture.cache_fixture.sidebar, self.fixture.cache_fixture.folder})
        self.assertEqual(len(result['relocations']), len(draft.METADATA_PATHS) + 2)
        self.assertEqual(result['parent_database_bytes_verified'],
                         {'CW': True, 'CCRD': False, 'RMRB': False, 'PLA': False})
        self.assertFalse(result['archive_bytes_verified'])
        self.assertFalse(result['upstream_provenance_verified'])
        self.assertFalse(result['cache_inventory_complete_proven'])
        self.assertFalse(result['generation_identity_profile_verified_by_production'])
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(result['explicit_deletions'], [])
        self.assertEqual(self.make(), result)

    def test_parent_summary_is_reference_only(self):
        summary = draft.inspect_parent(self.snapshot.pointer_bytes)
        self.assertEqual(summary['parent_manifest_sha256'], planner.sha(self.snapshot.pointer_bytes))
        self.assertEqual(summary['metadata_count'], len(draft.METADATA_PATHS))
        self.assertEqual(summary['archive_count'], 2)
        self.assertEqual(summary['databases'], ['CCRD', 'CW', 'PLA', 'RMRB'])
        self.assertFalse(summary['referenced_bytes_verified'])
        self.assertFalse(summary['publication_ready'])

    def test_same_size_change_keeps_unmodified_metadata_bytes(self):
        args = {**self.args, 'next_txt': b'x' * len(self.args['current_txt'])}
        snapshot = chain.capture(**args, specifications=self.fixture.specs, payloads=self.fixture.payloads)
        point = None
        for _ in range(4):
            point = chain.step(snapshot, point)
        result = draft.build(snapshot, point, remaining_metadata=self.remaining, next_source_url=source_override.source_urls()[0])
        self.assertEqual(set(result['space_changed_paths']),
                         {planner.TARGET_PATH, 'data/index-release.json', self.fixture.cache_fixture.initial})
        self.assertEqual(result['manifest']['base_metadata_sha256'], self.parent['base_metadata_sha256'])
        self.assertNotEqual(result['manifest']['databases']['CW']['sha256'], self.parent['databases']['CW']['sha256'])

    def test_cw_manifest_uses_updated_counts_not_parent_counts(self):
        result = self.make()
        cw = result['manifest']['databases']['CW']
        bundle = chain.materialize(self.snapshot, self.point)
        self.assertEqual({name: cw[name] for name in bundle['cw_counts']}, bundle['cw_counts'])
        self.assertEqual(cw['sha256'], planner.sha(bundle['cw_database']))
        self.assertEqual(cw['bytes'], len(bundle['cw_database']))
        self.assertTrue(cw['path'].endswith('/CW.sqlite3'))

    def test_missing_changed_or_extra_metadata_fails(self):
        for variant in ('missing', 'changed', 'extra'):
            remaining = dict(self.remaining)
            name = next(iter(remaining))
            if variant == 'missing':
                del remaining[name]
            elif variant == 'changed':
                remaining[name] += b'changed'
            else:
                remaining['data/unreviewed.json'] = b'{}'
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                draft.build(self.snapshot, self.point, remaining_metadata=remaining,
                            next_source_url=source_override.source_urls()[1])

    def test_tampered_checkpoint_and_noop_cannot_produce_a_manifest(self):
        with self.assertRaises(ValueError):
            draft.build(self.snapshot, replace(self.point, cw_sha256='0' * 64), remaining_metadata=self.remaining,
                        next_source_url=source_override.source_urls()[1])
        snapshot = replace(self.snapshot, next_txt=self.snapshot.current_txt)
        with self.assertRaises(ValueError):
            draft.build(snapshot, chain.step(snapshot), remaining_metadata=self.remaining,
                        next_source_url=source_override.source_urls()[1])

    def test_next_source_url_is_explicit_and_affects_generation(self):
        first = draft.build(self.snapshot, self.point, remaining_metadata=self.remaining,
                            next_source_url=source_override.source_urls()[0])
        second = self.make()
        self.assertNotEqual(first['generation'], second['generation'])
        self.assertEqual(first['manifest']['source_overrides'][planner.TARGET_PATH]['url'],
                         source_override.source_urls()[0])
        with self.assertRaisesRegex(ValueError, 'provenance'):
            draft.build(self.snapshot, self.point, remaining_metadata=self.remaining,
                        next_source_url='https://untrusted.example/text')

    def test_unknown_parent_schema_and_missing_archive_fail_closed(self):
        for mode in ('unknown-field', 'missing-archive', 'missing-metadata', 'unknown-cw-field',
                     'override-extra-field', 'override-wrong-size', 'metadata-extra-field', 'archive-extra-field',
                     'unknown-override'):
            parent = deepcopy(self.parent)
            if mode == 'unknown-field':
                parent['unreviewed'] = 'value'
            elif mode == 'missing-archive':
                parent['archives'].pop()
            elif mode == 'missing-metadata':
                parent['metadata'].pop()
            elif mode == 'unknown-cw-field':
                parent['databases']['CW']['unknown'] = 1
            elif mode == 'override-extra-field':
                parent['source_overrides'][planner.TARGET_PATH]['unknown'] = 'value'
            elif mode == 'override-wrong-size':
                parent['source_overrides'][planner.TARGET_PATH]['bytes'] += 1
            elif mode == 'metadata-extra-field':
                parent['metadata'][0]['unknown'] = 'value'
            elif mode == 'archive-extra-field':
                parent['archives'][0]['unknown'] = 'value'
            else:
                parent['source_overrides']['CW/unreviewed.txt'] = {'sha256': 'a' * 64}
            with self.subTest(mode=mode):
                self.set_parent(parent)
                with self.assertRaises(ValueError):
                    self.make()

    def test_archive_hash_is_only_a_claim_not_a_readback(self):
        # No archive content is supplied to the model. Matching declared hashes
        # are not proof that the real bytes exist or have been copied.
        result = self.make()
        self.assertTrue(all(not item['content_changed'] for item in result['relocations']
                            if item['kind'] == 'archive'))
        self.assertFalse(result['archive_bytes_verified'])


if __name__ == '__main__':
    unittest.main()
