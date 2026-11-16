"""Exercise the workflow entry point with real preparation and retirement logic."""
import base64
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, create_autospec, patch

from huggingface_hub import HfApi

from scripts import reader_obsolete_pdf_run as runner
from tests import test_reader_obsolete_pdf_apply as apply_fixtures
from tests.test_reader_route_control import Consumers


class ObsoletePDFRunTests(unittest.TestCase):
    def setUp(self):
        fixture = apply_fixtures.ObsoletePDFApplyTests()
        fixture.setUp()
        self.fixture = fixture.fixture
        self.store = fixture.store
        self.plan = fixture.plan
        self.ordered = runner.operation._ordered(self.plan['mutations'])
        self.before = copy.deepcopy(self.store.objects)
        self.pointer = runner.publication.current(self.fixture.store)[0]
        self.projection = copy.deepcopy(self.fixture.projection)
        self.metadata = copy.deepcopy(self.fixture.meta)
        temporary = tempfile.TemporaryDirectory(prefix='obsolete-pdf-run-test-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.config = directory / 'config.json'
        self.journal_path = directory / 'maintenance' / 'journal.json'
        self.output = directory / 'result' / 'obsolete-result.json'
        self.config.write_text(json.dumps({
            'inventory': self.fixture.inventory,
            'exact_copies': self.fixture.copies,
            'manifest_inventory': self.fixture.manifests,
            'ccrd_original': self.fixture.original,
        }), encoding='utf-8')
        self.state = {'version': 1, 'kind': 'reader-maintenance-journal',
                      'maintenance_id': 'test-window', 'repositories': {}}
        self.checkpoints = []
        self.cas_checkpoints = []
        self.events = []
        self.callback_result = None

        self.patch = lambda *args, **kwargs: self.enterContext(patch(*args, **kwargs))
        self.patch('os.environ', {
            'GITHUB_ACTIONS': 'true',
            'GITHUB_REPOSITORY': 'anftm/pipeline',
            'GITHUB_WORKFLOW_REF':
                'anftm/pipeline/.github/workflows/reader-maintenance.yml@refs/heads/main',
            'GITHUB_RUN_ID': '123456789',
            'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main',
        })
        self.patch('sys.argv', ['reader_obsolete_pdf_run', '--config', str(self.config),
                               '--journal', str(self.journal_path), '--output', str(self.output)])
        self.maintenance = self.patch(
            'scripts.reader_obsolete_pdf_run.maintenance.run', autospec=True,
            side_effect=self.isolated_run)
        # Keep the actual atomic local journal, but never construct a remote store.
        self.patch('scripts.reader_obsolete_pdf_run.maintenance.durable_store',
                   autospec=True, return_value=None)
        journal = runner.maintenance.journal

        def checkpoint(path, state):
            journal(path, state)
            self.checkpoints.append(json.loads(path.read_bytes()))
            self.events.append('journal')

        self.journal = self.patch('scripts.reader_obsolete_pdf_run.maintenance.journal',
                                  autospec=True, side_effect=checkpoint)
        self.reads = self.patch('scripts.reader_obsolete_pdf_run.CollectionStore',
                               autospec=True, return_value=self.fixture.store)
        self.hub = self.patch('scripts.reader_obsolete_pdf_run.HubBucketStore', autospec=True)
        self.api = create_autospec(HfApi, instance=True, spec_set=True)
        self.hub.return_value.api = self.api

        def metadata(bucket_id, paths, *, token=None):
            return [SimpleNamespace(size=self.metadata[path]['size'],
                                    xet_hash=self.metadata[path]['xet_hash'])
                    for path in paths if self.metadata[path]['bucket'] == bucket_id]

        self.api.get_bucket_paths_info.side_effect = metadata
        # The adapter's own tests cover S3 transport. Autospec preserves its real
        # constructor contract here; the shared fixture supplies conditional IO.
        self.adapter = self.patch('scripts.reader_obsolete_pdf_run.ObsoleteReferenceStore',
                                  autospec=True, return_value=self.store)
        self.fence = self.patch('scripts.reader_obsolete_pdf_run.GitHubProducerFence',
                                autospec=True)
        self.clear = self.patch('scripts.reader_obsolete_pdf_run.reader_gc_chm_window.require_clear',
                                autospec=True)
        self.client = self.patch('scripts.reader_obsolete_pdf_run.httpx.Client', autospec=True)
        self.client.return_value.__enter__.return_value = Consumers(self.fixture.store)
        self.before_graph = create_autospec(runner.graph.ReferenceGraph, instance=True)
        self.after_graph = create_autospec(runner.graph.ReferenceGraph, instance=True)
        self.graph = self.patch('scripts.reader_obsolete_pdf_run.graph.ReferenceGraph', autospec=True)
        self.before_report = {'graph_complete': True, 'blockers': []}
        self.after_report = {'graph_complete': True, 'blockers': []}
        self.before_graph.build.return_value = self.before_report
        self.after_graph.build.return_value = self.after_report
        self.graph.side_effect = [self.before_graph, self.after_graph]
        self.proof = {'kind': 'test-incoming-reference-proof', 'blockers': [],
                      'generation': self.plan['generation']}
        self.verify = self.patch('scripts.reader_obsolete_pdf_references.verify',
                                 autospec=True, return_value=self.proof)
        self.roots = self.patch('scripts.reader_obsolete_pdf_run.collector.verify_roots', autospec=True)
        self.documents = self.patch(
            'scripts.reader_obsolete_pdf_run.collector.verify_graph_documents', autospec=True)
        # These spies execute the real functions, including their API signatures.
        self.prepare = self.patch('scripts.reader_obsolete_pdf_run.preparation.prepare',
                                  autospec=True, side_effect=runner.preparation.prepare)
        self.observe = self.patch('scripts.reader_obsolete_pdf_run.publication.observe_consumer',
                                  autospec=True, side_effect=runner.publication.observe_consumer)
        self.store.on_stage = lambda bucket, path: self.events.append('intent')

        def before_cas(bucket, path):
            self.events.append(('cas', bucket, path))
            self.cas_checkpoints.append((json.loads(self.journal_path.read_bytes()),
                                         json.loads(self.output.read_bytes())))

        self.store.behavior = before_cas

    def isolated_run(self, path, *, isolated_callback, suppress_resume_dispatch,
                     preserve_queued_runs):
        self.assertEqual(path, self.journal_path)
        self.assertTrue(suppress_resume_dispatch)
        self.assertTrue(preserve_queued_runs)
        self.callback_result = isolated_callback(self.state)

    def assert_applied_checkpoints(self):
        """Check intent before staging, each pending CAS, readback, and completion."""
        operation = json.loads(self.output.read_bytes())['operation']
        self.assertEqual(operation['outcome'], 'applied')
        self.assertTrue(operation['applied'])
        self.assertTrue(operation['intent_verified'])
        self.assertFalse(operation['deletion_authorized'])
        self.assertEqual(operation['generation'], self.plan['generation'])
        self.assertEqual(len(operation['roots']), 9)
        self.assertEqual([(row['bucket'], row['path']) for row in operation['roots']],
                         [(m['bucket'], m['path']) for m in self.ordered])
        self.assertEqual([row['outcome'] for row in operation['roots']], ['applied'] * 9)
        self.assertEqual([row['cas_attempts'] for row in operation['roots']], [1] * 9)
        expected = copy.deepcopy(operation)
        expected.update(outcome='partial', applied=False, intent_verified=False)
        expected['roots'] = [dict(bucket=m['bucket'], path=m['path'],
                                  outcome='not_attempted', cas_attempts=0)
                             for m in self.ordered]
        snapshots = [copy.deepcopy(expected)]
        expected['intent_verified'] = True
        for row in expected['roots']:
            row.update(outcome='unknown', cas_attempts=1)
            snapshots.append(copy.deepcopy(expected))
            row.update(outcome='applied', recognized=False)
            snapshots.append(copy.deepcopy(expected))
        snapshots.append(operation)
        self.assertEqual([state['obsolete_pdf_operation'] for state in self.checkpoints[:20]],
                         snapshots)
        self.assertTrue(all('obsolete_pdf_acceptance' not in state
                            for state in self.checkpoints[:20]))
        expected_events = ['journal', 'intent']
        for m in self.ordered:
            expected_events.extend(['journal', ('cas', m['bucket'], m['path']), 'journal'])
        expected_events.append('journal')
        self.assertEqual(self.events[:len(expected_events)], expected_events)
        self.assertEqual(len(self.cas_checkpoints), 9)
        for index, (journal, output) in enumerate(self.cas_checkpoints):
            self.assertEqual(journal['obsolete_pdf_operation'], snapshots[1 + 2 * index])
            self.assertEqual(output, {'reference_proof': self.proof,
                                      'operation': snapshots[1 + 2 * index]})
        ref = operation['intent_ref']
        raw = self.store.read_bytes(ref['bucket'], ref['path'])
        self.assertEqual(runner.operation.proof(raw), {key: ref[key] for key in ('bytes', 'sha256')})
        self.assertEqual(ref['path'], runner.operation.INTENTS + ref['sha256'] + '.json')
        intent = json.loads(raw)
        self.assertEqual(intent['owner'], {'repository': 'anftm/pipeline',
                                          'run_id': '123456789', 'attempt': '1'})
        self.assertEqual(len(intent['mutations']), 9)
        for packed, mutation in zip(intent['mutations'], self.ordered):
            for phase in ('before', 'after'):
                self.assertEqual(base64.b64decode(packed[phase + '_base64']), mutation[phase])
                self.assertEqual(packed[phase + '_identity'], mutation[phase + '_identity'])
            actual = self.store.read_bytes(mutation['bucket'], mutation['path'])
            self.assertEqual(actual, mutation['after'])
            self.assertEqual(runner.operation.proof(actual), mutation['after_identity'])
        changed = {(m['bucket'], m['path']) for m in self.ordered}
        self.assertEqual({key: value for key, value in self.store.objects.items() if key in self.before
                          and key not in changed},
                         {key: value for key, value in self.before.items() if key not in changed})
        self.assertEqual(set(self.store.objects) - set(self.before), {(ref['bucket'], ref['path'])})
        self.assertEqual(self.store.events, [('intent', ref['bucket'], ref['path'])]
                         + [('cas', m['bucket'], m['path']) for m in self.ordered])
        return operation

    def test_main_checkpoints_all_nine_mutations_before_final_acceptance(self):
        runner.main()
        operation = self.assert_applied_checkpoints()
        result = json.loads(self.output.read_bytes())
        self.assertEqual(result, self.callback_result)
        self.assertEqual(result['reference_proof'], self.proof)
        self.assertEqual(len(self.checkpoints), 21)
        self.assertEqual(self.events[-1], 'journal')
        self.assertEqual(json.loads(self.journal_path.read_bytes()), self.state)
        acceptance = result['acceptance']
        self.assertEqual(self.checkpoints[-1]['obsolete_pdf_acceptance'], acceptance)
        self.assertEqual(self.state['obsolete_pdf_operation'], operation)
        inspection = acceptance['roots']
        self.assertEqual(inspection, runner.operation.inspect(self.store, operation['intent_ref']))
        self.assertEqual(inspection['roots'], [dict(bucket=m['bucket'], path=m['path'], outcome='after')
                                               for m in self.ordered])
        self.assertEqual(inspection['generation'], self.plan['generation'])
        self.assertFalse(inspection['deletion_authorized'])
        self.assertFalse(inspection['resume_authorized'])
        self.assertTrue(acceptance['graph_complete'])
        self.assertTrue(acceptance['active_pdfs_unchanged'])
        self.assertEqual(set(acceptance['observations']), {'hf', 'pages'})
        for site, observation in acceptance['observations'].items():
            self.assertEqual(observation['surface'], site)
            self.assertEqual(observation['generation'], self.plan['generation'])
            self.assertEqual(observation['catalog_sha256'], self.pointer['catalog']['sha256'])
            self.assertTrue(observation['projection_verified'])
            self.assertTrue(observation['checked_url'])
        self.assertEqual([args.args[1] for args in self.observe.call_args_list], ['hf', 'pages'] * 3)
        pointer, catalog = runner.publication.current(self.fixture.store)
        self.assertEqual(pointer, self.pointer)
        self.assertEqual(runner.publication.project_sidecar(
            runner.publication.pinned_base(self.fixture.store, catalog, None), catalog), self.projection)
        self.assertEqual(self.api.get_bucket_paths_info.call_args_list[-8:],
                         [call(row['pdf']['bucket'], [row['pdf']['path']])
                          for row in self.plan['replacement_proofs']])
        self.assertEqual(self.prepare.call_count, 2)
        self.adapter.assert_called_once()
        self.assertEqual(self.adapter.call_args.args[0], self.hub.return_value)
        self.assertEqual(self.adapter.call_args.args[1], self.plan['mutations'])
        self.verify.assert_called_once()
        self.assertEqual(self.verify.call_args.args[:2], (self.before_graph, self.before_report))
        prepared = self.verify.call_args.args[2]
        self.assertEqual(runner.operation._comparison(prepared), runner.operation._comparison(self.plan))
        self.assertEqual(self.graph.call_args_list, [call(self.fixture.store, workers=8)] * 2)
        self.before_graph.build.assert_called_once_with()
        self.after_graph.build.assert_called_once_with()
        for verification in (self.roots, self.documents):
            self.assertEqual(verification.call_args_list,
                             [call(self.fixture.store, self.before_graph),
                              call(self.fixture.store, self.after_graph)])
        self.maintenance.assert_called_once()
        self.clear.assert_called_once_with(self.fixture.store)
        self.assertEqual(self.fence.return_value.check.call_count, 14)
        self.client.assert_called_once_with(timeout=120, follow_redirects=True)
        self.client.return_value.__exit__.assert_called_once()

    def assert_after_graph_failure(self, report):
        self.after_graph.build.return_value = report
        with self.assertRaisesRegex(RuntimeError, 'after-state reference graph incomplete'):
            runner.main()
        self.assert_applied_checkpoints()
        result = json.loads(self.output.read_bytes())
        self.assertNotIn('acceptance', result)
        self.assertNotIn('obsolete_pdf_acceptance', self.state)
        self.assertEqual(len(self.checkpoints), 20)
        self.assertIsNone(self.callback_result)
        self.assertEqual([args.args[1] for args in self.observe.call_args_list], ['hf', 'pages'] * 2)
        for verification in (self.roots, self.documents):
            verification.assert_called_once_with(self.fixture.store, self.before_graph)
        self.after_graph.build.assert_called_once_with()
        self.client.return_value.__exit__.assert_called_once()

    def test_main_incomplete_after_graph_blocks_final_acceptance(self):
        self.assert_after_graph_failure({'graph_complete': False, 'blockers': []})

    def test_main_blocked_after_graph_blocks_final_acceptance(self):
        self.assert_after_graph_failure({'graph_complete': True, 'blockers': ['unresolved reference']})


if __name__ == '__main__':
    unittest.main()
