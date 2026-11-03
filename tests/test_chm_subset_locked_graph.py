import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml

from scripts import chm_subset_locked_graph as locked, chm_subset_locked_preflight as preflight


class LockedGraphTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'bucket': preflight.chm.ASSETS,
                      'path': f'native/ebook/chm/{n:064x}/document.epub',
                      'bytes': n, 'sha256': f'{n:064x}'} for n in range(1, 973)]
        self.selected, self.withheld = self.rows[600:952], self.rows[952:]
        self.old = [{'bucket': preflight.chm.ASSETS, 'status': 'verified-absent',
                     'paths': [row['path'] for row in self.rows[start:start + 100]]}
                    for start in range(0, 600, 100)]
        self.progress = {'batches': self.old, 'actual_graph': {'mode': 'actual'}}
        self.classification = json.dumps({'targets': [
            *({'path': row['path'], 'category': 'mapped-with-saved-evidence'} for row in self.selected),
            *({'path': row['path'], 'category': 'needs-independent-review'} for row in self.withheld)]}).encode()
        self.root_raw = b'original retention'
        self.store = Mock()
        self.store.assert_buckets_available = Mock()
        self.store.list_files.return_value = set()
        self.fence, self.client = Mock(), Mock()
        self.candidate = {'path': locked.isolated.root_path(locked.IDENTITY['transaction_id']),
                          'raw': b'synthetic-root', 'sha256': locked.ROOT_SHA, 'objects': 20}
        self.proof = {'mode': 'hypothetical-overlay', 'root_recheck_complete': False,
                      'reader_acceptance_complete': False, 'remote_mutations': 0, 'deletion_authorized': False}
        self.sites = {'hf': {'surface': 'hf', 'generation': 'a', 'catalog_sha256': 'b'},
                      'pages': {'surface': 'pages', 'generation': 'a', 'catalog_sha256': 'b'}}
        self.traversal = Mock()
        self.traversal.build.return_value = {'graph_complete': True}
        mocks = [patch.object(preflight, 'CLASSIFICATION_SHA', preflight.digest(self.classification)),
                 patch.object(preflight, '_identity', return_value=(b'p', b'm', self.root_raw, self.progress)),
                 patch.object(locked.journal, 'load_plan', return_value=(
                     {'overrides': {preflight.RETENTION: self.root_raw}}, {}, {}, preflight.PLAN_SHA)),
                 patch.object(locked.isolated, 'build', return_value=self.candidate),
                 patch.object(locked.overlay, 'view', return_value=(Mock(), self.candidate)),
                 patch.object(locked.graph, 'ReferenceGraph', return_value=self.traversal),
                 patch.object(locked.overlay, 'capture', return_value=self.proof),
                 patch.object(locked.isolated, 'compare', return_value={'mode': 'test-comparison'}),
                 patch.object(locked.collector, 'verify_roots'),
                 patch.object(preflight.safety, 'verify_live_chm_controls'),
                 patch.object(preflight.safety, 'verify_live_processing_controls'),
                 patch.object(preflight.safety, 'verify_retired_rollback_aliases'),
                 patch.object(preflight.safety, 'observe_planned_projection', return_value=self.sites)]
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def build(self):
        return locked.build(self.store, self.fence, self.client, self.rows, self.classification)

    def test_hypothetical_graph_never_claims_a_production_root_or_delete(self):
        result = self.build()
        self.assertEqual(result['candidate_objects'], 20)
        self.assertFalse(result['synthetic_root_published'])
        self.assertFalse(result['deletion_authorized'])
        self.assertFalse(result['producer_restore_authorized'])
        self.assertEqual(result['remote_mutations'], 0)
        locked.isolated.compare.assert_called_once()

    def test_original_controls_or_candidate_drift_stop(self):
        with patch.object(preflight, '_identity', side_effect=[
                (b'p', b'm', self.root_raw, self.progress), (b'changed', b'm', self.root_raw, self.progress)]):
            with self.assertRaisesRegex(ValueError, 'controls changed'):
                self.build()
        self.candidate['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'fixed synthetic protection differs'):
            self.build()

    def test_synthetic_path_appearing_in_storage_stops(self):
        self.store.list_files.return_value = {self.candidate['path']}
        with self.assertRaisesRegex(ValueError, 'appeared in production'):
            self.build()

    def test_site_drift_or_incomplete_graph_stops(self):
        later = {**self.sites, 'hf': {**self.sites['hf'], 'generation': 'changed'}}
        with patch.object(preflight.safety, 'observe_planned_projection', side_effect=[self.sites, later]):
            with self.assertRaisesRegex(ValueError, 'projection changed'):
                self.build()
        with patch.object(locked.overlay, 'capture', side_effect=ValueError('incomplete graph')):
            with self.assertRaisesRegex(ValueError, 'incomplete graph'):
                self.build()

    def test_actions_workflow_holds_three_groups_without_delete_command(self):
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        outer = yaml.load((root / 'chm-subset-locked-graph.yml').read_text(), Loader=yaml.BaseLoader)
        inner = yaml.load((root / 'chm-subset-locked-graph-inner.yml').read_text(), Loader=yaml.BaseLoader)
        groups = (outer['concurrency'], outer['jobs']['inspect']['concurrency'],
                  inner['jobs']['inspect']['concurrency'])
        self.assertEqual([group['group'] for group in groups],
                         ['publish-ccrd-index', 'reader-global-maintenance', 'reader-sidecar'])
        for group in groups:
            self.assertEqual((group['cancel-in-progress'], group['queue']), ('false', 'max'))
        commands = '\n'.join(step.get('run', '') for step in inner['jobs']['inspect']['steps'])
        self.assertIn('scripts/chm_subset_locked_graph.py', commands)
        self.assertNotIn('run_chm_epub_retirement.py', commands)


if __name__ == '__main__':
    unittest.main()
