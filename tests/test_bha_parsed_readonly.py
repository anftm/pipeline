import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts import check_bha_parsed_readonly as check


class BhaParsedReadOnlyTests(unittest.TestCase):
    def test_exact_tree_comparison_never_calls_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'baseline.json'
            state.write_text(json.dumps({'0': 'a' * 40}))
            calls = []
            def lookup(token, archive):
                calls.append((token, archive))
                return '0', 'b' * 40, 'a' * 40
            result = check.inspect('test', 0, state, lookup)
            self.assertEqual(calls, [('test', 0)])
            self.assertFalse(result['tree_changed'])
            self.assertFalse(result['reindex_requested'])
            self.assertEqual(result['remote_writes'], 0)
            result = check.inspect('test', 0, state, lambda *_: ('0', 'b' * 40, 'c' * 40))
            self.assertTrue(result['tree_changed'])
            self.assertEqual(json.loads(state.read_text()), {'0': 'a' * 40})

    def test_missing_invalid_or_unavailable_data_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'baseline.json'
            state.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'baseline'): check.inspect('test', 0, state)
            state.write_text('{"0":"' + 'a' * 40 + '"}')
            for lookup in (lambda *_: ('1', 'b' * 40, 'c' * 40), lambda *_: ('0', 'bad', 'c' * 40),
                           lambda *_: ('0', 'b' * 40, None)):
                with self.assertRaises(ValueError): check.inspect('test', 0, state, lookup)
            with self.assertRaises(ValueError): check.inspect('test', 32, state)
            with self.assertRaises(RuntimeError): check.inspect('', 0, state)
            with self.assertRaises(PermissionError):
                check.inspect('test', 0, state, lambda *_: (_ for _ in ()).throw(PermissionError('denied')))

    def test_all_archives_compare_ready_consumer_twice_without_writes(self):
        trees = {str(i): format(i, '040x') for i in range(32)}
        live = dict(trees); live['1'] = 'f' * 40
        def status(_token):
            return {'index_status': 'ready', 'switch_pending': False, 'archive_revisions': live}
        def lookup(_token, number):
            return str(number), format(number + 100, '040x'), trees[str(number)]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'baseline.json'; state.write_text(json.dumps(trees))
            result = check.inspect_all('github', 'bha', state, lookup, status)
            self.assertEqual(result['changed_from_baseline'], [])
            self.assertEqual(result['consumer_differences'], [1])
            self.assertEqual(len(result['archives']), 32)
            self.assertTrue(result['consumer_ready_observed_twice'])
            self.assertEqual(result['remote_writes'], 0)
            self.assertFalse(result['reindex_requested'])
            self.assertEqual(json.loads(state.read_text()), trees)

    def test_all_archives_reject_moving_unready_or_incomplete_consumer(self):
        trees = {str(i): format(i, '040x') for i in range(32)}
        def lookup(_token, number): return str(number), format(number + 100, '040x'), trees[str(number)]
        ready = {'index_status': 'ready', 'switch_pending': False, 'archive_revisions': trees}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'baseline.json'; state.write_text(json.dumps(trees))
            for status in ({**ready, 'index_status': 'starting'}, {**ready, 'switch_pending': True},
                           {**ready, 'archive_revisions': {'0': trees['0']}}):
                with self.subTest(status=status), self.assertRaises(ValueError):
                    check.inspect_all('github', 'bha', state, lookup, lambda _: status)
            moved = {**ready, 'archive_revisions': {**trees, '0': 'f' * 40}}
            observations = iter([ready, moved])
            with self.assertRaisesRegex(RuntimeError, 'changed during'):
                check.inspect_all('github', 'bha', state, lookup, lambda _: next(observations))
            with self.assertRaises(RuntimeError): check.inspect_all('github', '', state, lookup, lambda _: ready)

    def test_report_cannot_overwrite_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'baseline.json'; state.write_text('{}')
            with patch.object(check, 'inspect', return_value={'ok': True}), patch.dict('os.environ', {'GH_PAT': 'test'}):
                with self.assertRaisesRegex(ValueError, 'overwrite'):
                    check.main(['--archive', '0', '--state', str(state), '--output', str(state)])
            self.assertEqual(state.read_text(), '{}')

    def test_workflow_never_builds_parsed_or_requests_reindex(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-update-index.yml'
        flow = yaml.safe_load(path.read_text())
        self.assertEqual(set(flow.get('on', flow.get(True))), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        commands = '\n'.join(step.get('run', '') for job in flow['jobs'].values() for step in job['steps'])
        self.assertIn('check_bha_parsed_readonly.py', commands)
        for forbidden in ('build_fork_parsed.py', '/api/reindex', 'update_proofread_issues.py', 'git push'):
            self.assertNotIn(forbidden, commands)


if __name__ == '__main__':
    unittest.main()
