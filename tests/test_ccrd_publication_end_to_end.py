"""Simulate the entire CCRD staging/activation/pointer sequence without remote writes."""
import json
import unittest
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import ccrd_lifecycle as lifecycle
from scripts import ccrd_publication_coordinator as coord
from scripts import ccrd_space_activation as space
from scripts import ccrd_stage_manifest as manifest_stage
from scripts import ccrd_stage_objects as objects
from scripts import ccrd_staging_plan as planning
from scripts import ccrd_transaction as tx
from tests.test_ccrd_candidate_assembly import CandidateAssemblyTests
from tests.test_ccrd_metadata_patch import NEW, OLD
from tests.test_ccrd_pointer_transition import PointerMemory
from tests.test_ccrd_stage_manifest import ManifestMemoryStore
from tests.test_ccrd_transaction import MemoryStore as JournalMemory


REV, CHILD = 'c' * 40, 'e' * 40


class CcrdPublicationEndToEndTests(CandidateAssemblyTests):
    def build_fixture(self):
        # Stub only expensive SQLite audit on this tiny fixture; staging still
        # hashes actual local file bytes and manifest/Space checks remain real.
        with patch.object(planning.assembly, '_cw_database',
                          return_value=(objects._identity(self.candidate), {'seconds': 0.1})):
            description = self.make()
        candidate_plan = planning.make(self.pointer, description)
        for item in candidate_plan['objects']:
            if item['kind'] != 'archive':
                continue
            old_prefix = f"generations/{self.manifest['generation']}/"
            path = self.parent / item['source_path'][len(old_prefix):]
            path.parent.mkdir(parents=True, exist_ok=True)
            import gzip
            path.write_bytes(gzip.compress(b'checked complete year archive', mtime=0))
        return description, candidate_plan

    def test_staging_unknown_responses_space_activation_and_pointer_recovery(self):
        description, plan = self.build_fixture()
        staging = ManifestMemoryStore(self.pointer); staging.lose_response = True
        for item in plan['objects']:
            if item['source'] == 'parent':
                staging.preserved[item['source_path']] = {'bytes': item['bytes'], 'sha256': item['sha256']}
        for item in plan['identity']['preserved_databases'].values():
            staging.preserved[item['path']] = {'bytes': item['bytes'], 'sha256': item['sha256']}
        capture = lambda: {**plan['identity']['new_text'], 'revision': plan['identity']['source_revision'],
                           'url': plan['identity']['source_url'], 'source_stable': True}
        parent_consumer = lambda: self.observed
        staged = objects.stage(plan, approved_description=description, parent_root=self.parent,
                               candidate_root=self.output, candidate_cw=self.candidate, store=staging,
                               before_create=lambda: None)
        self.assertEqual(staged['objects_created'], len(plan['objects']))
        self.assertEqual(staging.creates, len(plan['objects']))
        self.assertFalse(staged['pointer_written'])
        staging.lost_response = True
        manifest = manifest_stage.stage_manifest(plan, approved_description=description, store=staging,
                                                  capture=capture, observe=parent_consumer, apply=True,
                                                  before_create=lambda: None)
        self.assertTrue(manifest['manifest_write_response_unknown'])
        self.assertEqual(staging.manifest_creates, 1)
        target = lifecycle.canonical(plan['manifest'])
        self.assertEqual(staging.raw_manifest, target)
        self.assertEqual(staging.pointer, self.pointer)

        api, journal, pointer = Mock(), JournalMemory(), PointerMemory(self.pointer)
        current = {'revision': REV}
        api.space_info.side_effect = lambda *_a, **_k: SimpleNamespace(sha=current['revision'])
        api.get_space_runtime.side_effect = lambda *_a, **_k: SimpleNamespace(stage='RUNNING', raw={'sha': current['revision']})
        api.get_space_variables.return_value = {}
        parent_activation = json.dumps({'version': 1, 'kind': 'ccrd-index-activation', 'bucket': lifecycle.BUCKET,
                                         'path': f"generations/{self.manifest['generation']}/manifest.json",
                                         'sha256': self.pointer_sha}).encode()
        activation_plan = None
        def get(url, _limit):
            revision = url.split('/resolve/', 1)[1].split('/', 1)[0]
            relative = urllib.parse.unquote(url.split('/resolve/' + revision + '/', 1)[1])
            if relative == 'data/ocr-release.json':
                raise urllib.error.HTTPError(url, 404, 'absent', {}, None)
            if relative == space.ACTIVATION:
                return parent_activation if revision == REV else lifecycle.canonical(activation_plan['activation'])
            if relative == str(space.source.CORPUS_PATH):
                return OLD.encode() if revision == REV else NEW.encode()
            return ((self.parent if revision == REV else self.output) / relative).read_bytes()
        activation_plan = space.plan(self.pointer, plan, description, api=api, space_revision=REV,
                                     store=staging, capture=capture, observe=parent_consumer, get=get,
                                     current_manifest=self.pointer, candidate_manifest=target)
        started = coord.prepare(journal, parent_pointer=self.pointer, candidate_plan=plan,
                                description=description, activation_plan=activation_plan,
                                pointer_store=pointer, transaction='1' * 32)
        self.assertEqual(started.record['state'], 'prepared')
        coord.start_space(journal)
        def commit(**_kwargs):
            current['revision'] = CHILD
            raise TimeoutError('Space committed but response was lost')
        api.create_commit.side_effect = commit
        apply_args = {'candidate_plan': plan, 'description': description, 'api': api, 'staging_store': staging,
                      'capture': capture, 'observe': parent_consumer, 'candidate_root': self.output,
                      'new_text': NEW.encode(), 'current_manifest': self.pointer, 'candidate_manifest': target, 'get': get}
        with self.assertRaises(TimeoutError):
            coord.apply_space_once(journal, **apply_args)
        self.assertEqual(tx.load(journal).record['state'], 'space-claimed')
        with self.assertRaisesRegex(ValueError, 'expected state'):
            coord.apply_space_once(journal, **apply_args)
        api.create_commit.assert_called_once()
        activated = {'kind': 'ccrd-space-activation-observation', 'revision': CHILD,
                     'generation': plan['generation'], 'manifest_sha256': plan['manifest_sha256'],
                     'runtime_observed_twice': True, 'activation': activation_plan['activation']}
        consumed = {'generation': plan['generation'], 'cw0': plan['identity']['new_text'],
                    'cw0_bytes_verified': True, 'status_observed_twice': True,
                    'loaded_sources': ['CCRD', 'CW', 'PLA', 'RMRB']}
        activation_read = lambda *_a, **_k: activated
        consumer_read = lambda *_a: consumed
        observed = coord.observe_space(journal, api=api, get=get, child_of=lambda p, c: (p, c) == (REV, CHILD),
                                       observe_activation=activation_read, observe_consumer=consumer_read)
        self.assertTrue(observed['journal_advanced'])
        self.assertEqual(tx.load(journal).record['state'], 'space-verified')
        coord.start_pointer(journal)
        pointer.lost = True
        staging.generation = plan['generation']
        pointer_args = {'candidate_plan': plan, 'description': description, 'staging_store': staging,
                        'api': api, 'capture': capture, 'observe_activation': activation_read,
                        'observe_consumer': consumer_read}
        result = coord.apply_pointer_once(journal, pointer, **pointer_args)
        self.assertEqual(result['status'], 'written-response-unknown')
        self.assertEqual(pointer.writes, 1)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-claimed')
        with self.assertRaisesRegex(ValueError, 'expected state'):
            coord.apply_pointer_once(journal, pointer, **pointer_args)
        reconciled = coord.observe_pointer(journal, pointer, **pointer_args)
        self.assertTrue(reconciled['journal_advanced'])
        self.assertEqual(tx.load(journal).record['state'], 'pointer-verified')
        self.assertEqual(pointer.raw, target)
        self.assertEqual(pointer.writes, 1)
        self.assertEqual(staging.pointer, self.pointer)  # separate fake, no production bucket
        self.assertEqual(tx.audit(journal, tx.load(journal)), 7)
        staging.generation = plan['generation']
        finish_args = {key: value for key, value in pointer_args.items() if key != 'immutable_manifest'}
        finish_args['staging_store'] = staging
        completed = coord.finish(journal, pointer, **finish_args)
        self.assertEqual(completed.record['state'], 'complete')
        self.assertEqual(tx.audit(journal, completed), 8)
        self.assertEqual(pointer.writes, 1)
        self.assertEqual(set(plan['manifest']['databases']), {'CCRD', 'CW', 'RMRB', 'PLA'})
        self.assertEqual(len(plan['manifest']['metadata']), 17)
        self.assertEqual(len(plan['manifest']['archives']), len(self.manifest['archives']))


if __name__ == '__main__':
    unittest.main()
