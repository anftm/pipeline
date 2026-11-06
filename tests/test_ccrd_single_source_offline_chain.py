"""End-to-end *synthetic* snapshot, candidate and restart model tests."""
from copy import deepcopy
from dataclasses import replace
import gzip
import json
import re
import sqlite3
import unittest
from unittest.mock import patch

from scripts import check_ccrd_txt_only as checker
from scripts import ccrd_single_source_cw_shadow as shadow
from scripts import ccrd_single_source_offline_chain as chain
from scripts import ccrd_single_source_plan as planner
from tests import test_ccrd_single_source_cw_candidate as index_fixture
from tests import test_ccrd_single_source_surfaces as cache_fixture
from tests import test_check_ccrd_txt_only as check_fixture


@unittest.skipUnless(hasattr(sqlite3.Connection, 'serialize') and hasattr(sqlite3.Connection, 'deserialize'),
                     'SQLite byte serialization requires a supported Python build')
class OfflineChainTests(unittest.TestCase):
    def setUp(self):
        self.index_fixture = index_fixture.CwCandidateTests('test_noop_returns_exact_original_bytes')
        self.index_fixture.setUp()
        self.addCleanup(self.index_fixture.doCleanups)
        self.args = self.index_fixture.args()
        self.cache_fixture = cache_fixture.SingleSourceSurfaceTests('test_unchanged_txt_keeps_all_original_bytes')
        self.cache_fixture.setUp()
        current = self.args['current_txt']
        self.cache_fixture.data[self.cache_fixture.initial]['results'][0].update(
            Size=len(current), snippet=re.sub(r'\s+', ' ', current.decode()).strip()[:700])
        self.cache_fixture.data[self.cache_fixture.sidebar]['files'][0]['size'] = len(current)
        self.cache_fixture.data[self.cache_fixture.folder]['CW']['']['files'][0]['size'] = len(current)
        self.specs, self.payloads = self.cache_fixture.inventory()
        self.snapshot = chain.capture(**self.args, specifications=self.specs, payloads=self.payloads)

    def all_stages(self, snapshot=None):
        snapshot = snapshot or self.snapshot
        checkpoints = []
        point = None
        for _ in range(4):
            point = chain.step(snapshot, point)
            checkpoints.append(point)
        return checkpoints

    def test_same_txt_drives_target_index_metadata_and_cache_without_other_changes(self):
        inputs_before = deepcopy((self.args, self.specs, self.payloads))
        points = self.all_stages()
        self.assertEqual([point.stage for point in points], ['planned', 'indexed', 'cached', 'prepared'])
        self.assertEqual(len({point.snapshot_sha256 for point in points}), 1)
        self.assertEqual(chain.step(self.snapshot, points[-1]), points[-1])
        bundle = chain.materialize(self.snapshot, points[-1])
        self.assertEqual(bundle['target_path'], planner.TARGET_PATH)
        self.assertEqual(bundle['target_txt'], self.args['next_txt'])
        self.assertFalse(bundle['publication_ready'])
        self.assertFalse(bundle['inventory_complete_proven'])
        self.assertEqual(bundle['remote_writes'], 0)
        self.assertEqual(bundle['explicit_deletions'], [])
        self.assertEqual((self.args, self.specs, self.payloads), inputs_before)

        database = self.index_fixture.open_bytes(bundle['cw_database'])
        expected = shadow.build({**self.index_fixture.documents, 'CW:0': self.args['next_txt'].decode()})
        self.assertEqual(self.index_fixture.sqlite_fixture.observation(database), expected)
        metadata = json.loads(gzip.decompress(bundle['metadata_gz']))
        old_metadata = json.loads(gzip.decompress(self.args['metadata_gz']))
        expected_metadata = deepcopy(old_metadata)
        target = next(record for record in expected_metadata['records'] if record['doc_id'] == 'CW:0')
        target['size'] = len(self.args['next_txt'])
        self.assertEqual(metadata, expected_metadata)
        cache = bundle['cache_payloads']
        initial = json.loads(cache[self.cache_fixture.initial])['results'][0]
        self.assertEqual(initial['Size'], len(self.args['next_txt']))
        self.assertEqual(initial['snippet'], re.sub(r'\s+', ' ', self.args['next_txt'].decode()).strip()[:700])
        self.assertEqual(cache[self.cache_fixture.other], self.payloads[self.cache_fixture.other])
        self.assertEqual(set(bundle['changed_cache_paths']),
                         {self.cache_fixture.initial, self.cache_fixture.sidebar, self.cache_fixture.folder})
        self.assertEqual(points[-1].cw_sha256, planner.sha(bundle['cw_database']))
        self.assertEqual(dict(points[-1].cache_sha256), {name: planner.sha(raw) for name, raw in cache.items()})

    def test_no_change_skips_candidate_and_cache_generation(self):
        snapshot = replace(self.snapshot, next_txt=self.snapshot.current_txt)
        with patch.object(chain.index, 'build_candidate') as index, patch.object(chain.surfaces, 'update_payloads') as cache:
            point = chain.step(snapshot)
            self.assertEqual(point.stage, 'unchanged')
            self.assertFalse(point.publication_ready)
            self.assertEqual(chain.step(snapshot, point), point)
            with self.assertRaisesRegex(ValueError, 'prepared'):
                chain.materialize(snapshot, point)
            index.assert_not_called()
            cache.assert_not_called()

    def test_detection_response_bytes_are_the_bytes_used_for_candidates(self):
        check = check_fixture.CcrdTxtOnlyTests('test_matching_text_has_no_write_or_publish_authority')
        check.setUp()
        check.revision = self.args['space_revision']
        check.manifest = json.loads(self.args['pointer_bytes'])
        check.manifest['generation'] = check.generation = 'ccrd-four-' + 'a' * 64
        check.manifest['base_metadata_sha256'] = planner.sha(self.args['metadata_gz'])
        check.manifest_raw = json.dumps(check.manifest, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode() + b'\n'
        check.activation = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': checker.BUCKET,
                            'path': f'generations/{check.generation}/manifest.json',
                            'sha256': planner.sha(check.manifest_raw)}
        check.status['bucket']['generation'] = check.generation
        check.status['bucket']['base_metadata_sha256'] = planner.sha(self.args['metadata_gz'])
        check.site = self.args['current_txt']
        check.upstream = self.args['next_txt']
        observed = checker.observe(read=check.read)
        self.assertFalse(observed['report']['upstream_matches_site'])
        snapshot = chain.capture(space_revision=observed['report']['space_revision'],
                                 pointer_bytes=observed['manifest_bytes'], activation_bytes=observed['activation_bytes'],
                                 metadata_gz=self.args['metadata_gz'], current_txt=observed['site_txt'],
                                 next_txt=observed['upstream_txt'], database_bytes=self.args['database_bytes'],
                                 specifications=self.specs, payloads=self.payloads)
        # Mutating the source fixture after its single GET must not change the
        # candidate; no second upstream download occurs in the offline chain.
        check.upstream = b'a later upstream version'
        bundle = chain.materialize(snapshot, self.all_stages(snapshot)[-1])
        self.assertEqual(bundle['target_txt'], self.args['next_txt'])
        self.assertEqual(planner.sha(bundle['target_txt']), observed['report']['upstream_txt_sha256'])
        self.assertEqual(len([url for url, _ in check.calls if url.startswith('https://raw.githubusercontent.com/')]), 1)
        self.assertFalse(bundle['publication_ready'])

    def test_interruptions_and_lost_local_responses_are_deterministic(self):
        expected = self.all_stages()
        # A purely in-memory stand-in for state recording: interruption before
        # or after assignment is not evidence about actual remote persistence.
        for lost_at in range(4):
            for saved in (False, True):
                state = {'point': None}
                for i in range(lost_at):
                    state['point'] = chain.step(self.snapshot, state['point'])
                prior = state['point']
                result = chain.step(self.snapshot, prior)
                if saved:
                    state['point'] = result
                # Response is lost; caller resumes from whichever local state
                # was recorded, not by performing or retrying remote writes.
                if not saved:
                    self.assertEqual(chain.step(self.snapshot, prior), result)
                    state['point'] = chain.step(self.snapshot, prior)
                self.assertEqual(state['point'], expected[lost_at])
                for remaining in expected[lost_at + 1:]:
                    state['point'] = chain.step(self.snapshot, state['point'])
                    self.assertEqual(state['point'], remaining)
                self.assertEqual(chain.materialize(self.snapshot, state['point'])['checkpoint'], expected[-1])

    def test_input_drift_at_each_stage_is_rejected(self):
        points = self.all_stages()
        first_path, raw = self.snapshot.cache_payloads[0]
        variants = [replace(self.snapshot, space_revision='c' * 40),
                    replace(self.snapshot, current_txt=b'different current text'),
                    replace(self.snapshot, next_txt=b'different new text'),
                    replace(self.snapshot, metadata_gz=self.snapshot.metadata_gz + b'x'),
                    replace(self.snapshot, pointer_bytes=self.snapshot.pointer_bytes + b' '),
                    replace(self.snapshot, activation_bytes=self.snapshot.activation_bytes + b' '),
                    replace(self.snapshot, database_bytes=self.snapshot.database_bytes + b'x'),
                    replace(self.snapshot, inventory_json=self.snapshot.inventory_json + b' '),
                    replace(self.snapshot, cache_payloads=((first_path, raw + b' '),) + self.snapshot.cache_payloads[1:])]
        for point in points:
            for variant in variants:
                with self.subTest(stage=point.stage, variant=variants.index(variant)), self.assertRaises(ValueError):
                    chain.step(variant, point)
        with self.assertRaises(ValueError):
            chain.materialize(variants[2], points[-1])

    def test_inconsistent_checkpoint_evidence_and_missing_cache_are_rejected(self):
        points = self.all_stages()
        for point in points:
            for tampered in (replace(point, snapshot_sha256='0' * 64),
                             replace(point, plan_sha256='0' * 64),
                             replace(point, publication_ready=True)):
                if tampered == point:
                    continue
                with self.subTest(stage=point.stage, tampered=tampered), self.assertRaises(ValueError):
                    chain.step(self.snapshot, tampered)
        bad = replace(points[-1], cache_sha256=points[-1].cache_sha256[:-1])
        with self.assertRaises(ValueError):
            chain.materialize(self.snapshot, bad)
        with self.assertRaises(ValueError):
            chain.step(self.snapshot, replace(points[-1], stage='unknown'))
        with self.assertRaises(ValueError):
            chain.capture(**self.args, specifications=self.specs,
                          payloads={name: raw for name, raw in self.payloads.items()
                                    if name != self.cache_fixture.folder})

    def test_stage_label_is_forgeable_and_does_not_prove_persistence(self):
        cached = self.all_stages()[2]
        forged = replace(cached, stage='prepared')
        # This is deliberately not treated as proof of a persisted prepared
        # state: materialize rebuilds all local bytes, and never publishes.
        with patch.object(chain.index, 'build_candidate', wraps=chain.index.build_candidate) as rebuild:
            bundle = chain.materialize(self.snapshot, forged)
        self.assertEqual(rebuild.call_count, 1)
        self.assertFalse(bundle['publication_ready'])
        self.assertEqual(bundle['remote_writes'], 0)

    def test_injected_stage_failures_can_resume_only_from_prior_local_state(self):
        planned, indexed, cached, prepared = self.all_stages()
        for prior, expected, module, name in (
                (planned, indexed, chain.index, 'build_candidate'),
                (indexed, cached, chain.surfaces, 'update_payloads'),
                (cached, prepared, chain.surfaces, 'update_payloads')):
            with self.subTest(stage=expected.stage), patch.object(module, name, side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    chain.step(self.snapshot, prior)
            self.assertEqual(chain.step(self.snapshot, prior), expected)

    def test_invalid_index_or_caches_cannot_advance_past_their_stages(self):
        broken_db = replace(self.snapshot, database_bytes=self.snapshot.database_bytes + b'x')
        planned = chain.step(broken_db)
        with self.assertRaisesRegex(ValueError, 'database bytes'):
            chain.step(broken_db, planned)
        bad_specs = deepcopy(self.specs)
        bad_specs[self.cache_fixture.initial]['positions'] = []
        bad_caches = chain.capture(**self.args, specifications=bad_specs, payloads=self.payloads)
        indexed = chain.step(bad_caches, chain.step(bad_caches))
        with self.assertRaisesRegex(ValueError, 'inventory'):
            chain.step(bad_caches, indexed)

    def test_same_byte_length_change_still_builds_cw_candidate(self):
        new = b'x' * len(self.snapshot.current_txt)
        snapshot = replace(self.snapshot, next_txt=new)
        point = self.all_stages(snapshot)[-1]
        bundle = chain.materialize(snapshot, point)
        self.assertEqual(bundle['target_txt'], new)
        self.assertEqual(bundle['metadata_gz'], snapshot.metadata_gz)
        self.assertNotEqual(bundle['cw_database'], snapshot.database_bytes)
        self.assertEqual(bundle['changed_cache_paths'], [self.cache_fixture.initial])

    def test_capture_freezes_caller_inventory_and_rejects_ambiguous_data(self):
        prior = chain.step(self.snapshot)
        self.specs[self.cache_fixture.initial]['positions'].clear()
        self.payloads[self.cache_fixture.initial] = b'changed'
        self.assertEqual(chain.step(self.snapshot), prior)
        for spec in ({**self.specs, 3: {}}, {'x': {'nested': {3: 4}}}, {'x': {'size': float('nan')}}):
            payloads = {name: b'{}' for name in spec}
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                chain.capture(**self.args, specifications=spec, payloads=payloads)


if __name__ == '__main__':
    unittest.main()
