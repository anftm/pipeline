"""Index planning is read-only, requires stable 32-archive identities."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import bha_reindex_plan as subject


class BhaReindexPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.baseline = {str(i): 'a' * 40 for i in range(32)}
        self.path.write_text(json.dumps(self.baseline))
        self.status = {'index_status': 'ready', 'switch_pending': False, 'archive_revisions': dict(self.baseline),
                       'build_fingerprint': 'fingerprint', 'job': {'state': 'succeeded', 'job_id': 'job', 'mode': 'full'}}
        self.health = json.dumps({'ok': True, 'index_ready': True, 'index_status': 'ready',
                                  'document_count': 9, 'es': True}).encode()
        self.calls = []
        self.lookup = lambda _token, index: (str(index), 'b' * 40, 'a' * 40)
        self.read = lambda _token: self.status
        self.fetch = lambda *_args: self.health

    def inspect(self, **changes):
        args = {'lookup': self.lookup, 'read_status': self.read, 'fetch': self.fetch, **changes}
        return subject.inspect('fixture', 'fixture', self.path, **args)

    def test_equal_trees_do_not_request_incremental_or_full_index(self):
        value = self.inspect()
        self.assertEqual(value['incremental_action'], 'no-change')
        self.assertEqual(value['changed_archives'], [])
        self.assertEqual(value['baseline_drift'], [])
        self.assertEqual(value['full_rebuild'], 'not-requested')
        self.assertEqual(value['remote_writes'], 0); self.assertFalse(value['reindex_requested'])
        self.assertFalse(value['publication_ready']); self.assertFalse(value['external_writers_verified'])

    def test_changed_trees_are_listed_exactly_but_not_dispatched(self):
        def lookup(_token, index): return str(index), 'b' * 40, ('c' * 40 if index in {0, 25} else 'a' * 40)
        value = self.inspect(lookup=lookup)
        self.assertEqual(value['changed_archives'], [0, 25])
        self.assertEqual(value['baseline_drift'], [0, 25])
        self.assertEqual(value['incremental_action'], 'review-changes')
        self.assertFalse(value['reindex_requested'])

    def test_moving_sources_consumer_or_active_job_fail_closed(self):
        def lookup(_token, index):
            self.calls.append(index)
            return str(index), 'b' * 40, ('c' * 40 if len(self.calls) > 32 and index == 0 else 'a' * 40)
        with self.assertRaisesRegex(ValueError, 'changed during'):
            self.inspect(lookup=lookup)
        changed = {**self.status, 'build_fingerprint': 'other'}
        values = iter([self.status, changed])
        with self.assertRaisesRegex(ValueError, 'consumer changed'):
            self.inspect(read_status=lambda *_: next(values))
        for job in ({'state': 'running', 'job_id': 'job'}, {'state': 'accepted', 'job_id': 'job'}, {}):
            with self.subTest(job=job), self.assertRaisesRegex(ValueError, 'active or has unknown'):
                self.inspect(read_status=lambda *_: {**self.status, 'job': job})

    def test_missing_identity_credentials_or_bad_health_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'credentials'):
            subject.inspect('', 'token', self.path)
        bad = dict(self.baseline); bad.pop('31')
        with self.assertRaisesRegex(ValueError, 'all 32'):
            self.inspect(read_status=lambda *_: {**self.status, 'archive_revisions': bad})
        with self.assertRaisesRegex(ValueError, 'not ready'):
            self.inspect(fetch=lambda *_: b'{}')
        with self.assertRaisesRegex(ValueError, 'incomplete or invalid'):
            self.inspect(lookup=lambda _token, i: (str(i), 'bad', 'a' * 40))

    def test_pinned_samples_must_match_exact_sources(self):
        sample = {'archive': 0, 'commit': 'b' * 40, 'tree': 'a' * 40}
        with patch.object(subject.samples, 'observe', return_value={'samples': [sample]}) as observe:
            result = self.inspect(selected_samples=[{'archive': 0, 'path': 'fixture'}])
        self.assertEqual(result['sample_count'], 1); observe.assert_called_once()
        sample['tree'] = 'c' * 40
        with patch.object(subject.samples, 'observe', return_value={'samples': [sample]}):
            with self.assertRaisesRegex(ValueError, 'sample differs'):
                self.inspect(selected_samples=[{'archive': 0, 'path': 'fixture'}])

    def test_workflow_manual_readonly_no_reindex_secret_exposure(self):
        flow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/bha-reindex-plan.yml').read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'update-bha-index')
        steps = flow['jobs']['inspect']['steps']; commands = '\n'.join(x.get('run', '') for x in steps)
        self.assertIn('scripts/bha_reindex_plan.py', commands)
        self.assertNotIn('/api/reindex', commands)
        self.assertNotIn('git push', commands)
        self.assertNotIn('ccrd_auto_activate', commands)


if __name__ == '__main__':
    unittest.main()
