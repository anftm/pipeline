import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import reader_ebook_processing_handoff as handoff


class ExactEbookHandoffTests(unittest.TestCase):
    def setUp(self):
        self.plan = handoff.load_manifest()

    def write_plan(self, value, directory):
        path = Path(directory) / 'inventory.json'
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
        path.write_bytes(raw)
        return path, handoff.digest(raw)

    def test_checked_in_inventory_is_exact_and_content_addressed(self):
        self.assertEqual(len(self.plan['entries']), 51)
        self.assertEqual(len({x['processing_path'] for x in self.plan['entries']}), 51)
        self.assertEqual(self.plan['source_run_id'], handoff.SOURCE_RUN)
        self.assertEqual(self.plan['v3_generation'], handoff.V3_GENERATION)
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.write_plan(self.plan, directory)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                handoff.load_manifest(path, expected_sha256='0' * 64)

    def test_inventory_rejects_scope_widening_and_original_route_mutation(self):
        for alter in ('extra', 'duplicate', 'foreign-path', 'source', 'other-route'):
            value = copy.deepcopy(self.plan)
            if alter == 'extra':
                value['entries'].append(copy.deepcopy(value['entries'][0]))
            elif alter == 'duplicate':
                value['entries'][-1] = copy.deepcopy(value['entries'][0])
            elif alter == 'foreign-path':
                value['entries'][0]['processing_path'] = 'reader-index/processing/stream-upload-' + 'f' * 32 + '.json'
            elif alter == 'source':
                value['entries'][0]['source_sha256'] = 'f' * 64
            else:
                value['entries'][0]['after']['f'] = 'native/ebook/chm/' + 'a' * 64 + '/document.epub'
            with self.subTest(alter=alter), tempfile.TemporaryDirectory() as directory:
                path, digest = self.write_plan(value, directory)
                with self.assertRaises(ValueError):
                    handoff.load_manifest(path, expected_sha256=digest)

    def test_mutation_requires_exact_workflow_context(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'central locked workflow'):
                handoff.assert_workflow_context()
        context = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
                   'GITHUB_WORKFLOW_REF': handoff.WORKFLOW, 'GITHUB_RUN_ID': '123'}
        with patch.dict('os.environ', context, clear=True):
            handoff.assert_workflow_context()
        with patch.dict('os.environ', {**context, 'GITHUB_WORKFLOW_REF': 'other'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'central locked workflow'):
                handoff.assert_workflow_context()

    def test_preview_never_writes_or_requires_production_context(self):
        result = {'planned': 1, 'already_released': 50, 'complete': False, 'remote_mutations': 0}
        with patch.object(handoff, 'prepare', return_value=(object(), [], result)), \
             patch.object(handoff, 'assert_workflow_context', side_effect=AssertionError('unexpected mutation')):
            self.assertIs(handoff.run(object(), object(), self.plan, apply=False), result)

    def test_exact_resume_releases_only_protection_and_checks_controls(self):
        row = self.plan['entries'][0]
        path = row['processing_path']
        generation = handoff.BASE_GENERATION
        record = {'status': 'released', 'generation': generation,
                  'run': {'repository': 'anftm/pipeline', 'run_id': handoff.SOURCE_RUN,
                          'attempt': handoff.SOURCE_ATTEMPT}}
        events = []

        class Writer:
            def publish(self, path, value):
                events.append(('publish', path))
            def read(self, path):
                return record

        class Store:
            def __init__(self):
                self.files = {}
            def put_bytes(self, bucket, path, raw):
                self.files[path] = raw
                events.append(('receipt', path))
            def read_bytes(self, bucket, path):
                if path == 'reader-index/gc/maintenance/current.json':
                    return b'{"restored":true,"maintenance_id":"stable"}'
                return self.files[path]

        result = {'planned': 1, 'already_released': 0, 'complete': False, 'remote_mutations': 0,
                  'index_sha256': {}, 'catalog_identity': {'current': generation}, 'maintenance_id': 'stable'}
        state = {'pointer': {'generation': handoff.V3_GENERATION}, 'catalog_identity': result['catalog_identity'],
                 'maintenance_id': 'stable', 'consumers': {'hf': {}, 'pages': {}}}
        plan = {**self.plan, 'entries': [row]}
        context = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
                   'GITHUB_WORKFLOW_REF': handoff.WORKFLOW, 'GITHUB_RUN_ID': '123'}
        with patch.dict('os.environ', context, clear=True), \
             patch.object(handoff, 'prepare', return_value=(Writer(), [(path, record)], result)), \
             patch.object(handoff, 'observe_state', return_value=state), \
             patch.object(handoff, 'assert_no_foreign_producers'):
            final = handoff.run(Store(), object(), plan, apply=True)
        publishes = [x for x in events if x[0] == 'publish']
        self.assertEqual(publishes, [('publish', path)])
        self.assertEqual(final['remote_mutations'], 1)
        self.assertTrue(final['complete'])
        self.assertEqual(final['verified'], 1)

    def test_exact_workflow_uses_shared_lock_and_does_not_cancel(self):
        source = (Path(__file__).resolve().parents[1] / '.github/workflows/reader-ebook-processing-handoff.yml').read_text()
        self.assertIn('group: reader-sidecar', source)
        self.assertIn('cancel-in-progress: false', source)
        self.assertNotIn('gh run cancel', source)
        self.assertNotIn('reader-gc-collect', source)

    def test_active_producer_blocks_without_touching_queued_runs(self):
        fence = unittest.mock.Mock()
        fence.api.side_effect = lambda endpoint: [{'workflow_runs': [
            {'id': 456, 'status': 'in_progress', 'path': '.github/workflows/reader-image-stream.yml'}]
            if 'status=in_progress' in endpoint else []}]
        with patch.dict('os.environ', {'GITHUB_RUN_ID': '123'}, clear=True), \
             patch.object(handoff, 'producer_inventory', return_value={'anftm/pipeline': {}}), \
             patch.object(handoff, 'GitHubProducerFence', return_value=fence):
            with self.assertRaisesRegex(RuntimeError, 'foreign Reader producer'):
                handoff.assert_no_foreign_producers()
        self.assertTrue(all('status=queued' not in call.args[0] for call in fence.api.call_args_list))


if __name__ == '__main__':
    unittest.main()
