import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scripts import reader_gc_graph as gc


class ReaderGcRetryTests(unittest.TestCase):
    def test_deadline_terminates_stuck_child_and_writes_incomplete_report(self):
        def timeout(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], 60)
            self.assertNotIn('--record-observations', args[0])
            self.assertNotIn('--deadline-minutes', args[0])
            raise subprocess.TimeoutExpired(args[0], 60)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            with patch('sys.argv', ['reader_gc_graph.py', '--output', str(output), '--deadline-minutes', '1',
                                    '--record-observations', '--allow-incomplete']), \
                    patch('subprocess.run', side_effect=timeout), patch.object(gc, 'HubBucketStore') as store:
                self.assertEqual(gc.main(), 0)
            store.assert_not_called()
            report = json.loads(output.read_text())
            self.assertFalse(report['graph_complete'])
            self.assertFalse(report['deletion_authorized'])
            self.assertEqual(report['buckets'], {})
            self.assertTrue(any('deadline' in blocker for blocker in report['blockers']))

    def test_completed_child_report_only_parent_records_observations(self):
        complete = {'version': 1, 'mode': 'report-only', 'graph_complete': True, 'blockers': [], 'buckets': {}}
        def child(command, **_kwargs):
            Path(command[command.index('--output') + 1]).write_text(json.dumps(complete))
            self.assertNotIn('--record-observations', command)
            return subprocess.CompletedProcess(command, 0)
        store = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch('sys.argv', ['reader_gc_graph.py', '--output', str(Path(directory) / 'report.json'),
                                   '--deadline-minutes', '1', '--record-observations']), \
                patch('subprocess.run', side_effect=child), patch.object(gc, 'HubBucketStore', return_value=store), \
                patch.object(gc, 'read_gc_state', return_value={'version': 1, 'kind': 'reader-gc-state', 'orphans': {}}), \
                patch.object(gc, 'plan_retention', return_value=({'orphans': {}}, {'marked': 0})):
            self.assertEqual(gc.main(), 0)
        store.put_json.assert_called_once()

    def test_real_child_process_with_stuck_thread_is_killed_at_timeout(self):
        script = 'from concurrent.futures import ThreadPoolExecutor; import time; p=ThreadPoolExecutor(1); p.submit(time.sleep, 8); p.shutdown(wait=False)'
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run([sys.executable, '-c', script], timeout=0.4, check=False)
        self.assertLess(time.monotonic() - start, 3)

    def test_transient_snapshot_race_is_retried(self):
        incomplete = {"graph_complete": False, "blockers": ["root inventory did not stabilize after 6 rounds"],
                      "buckets": {}, "missing": []}
        complete = {"graph_complete": True, "blockers": [], "buckets": {}, "missing": []}
        with patch.object(gc, "HubBucketStore", return_value=Mock()), \
                patch.object(gc.ReferenceGraph, "build", side_effect=[incomplete, complete]), \
                patch.object(gc.time, "sleep") as sleep, \
                patch.object(gc, "read_gc_state", return_value={"version": 1, "kind": "reader-gc-state", "orphans": {}}), \
                patch.object(gc, "plan_retention", return_value=({}, {})):
            with patch("sys.argv", ["reader_gc_graph.py", "--output", "/tmp/gc-retry.json"]):
                self.assertEqual(gc.main(), 0)
        sleep.assert_called_once_with(1)

    def test_structural_blocker_is_not_retried(self):
        incomplete = {"graph_complete": False, "blockers": ["unknown root schema: bucket:path"],
                      "buckets": {}, "missing": []}
        with patch.object(gc, "HubBucketStore", return_value=Mock()), \
                patch.object(gc.ReferenceGraph, "build", return_value=incomplete) as build, \
                patch.object(gc.time, "sleep") as sleep:
            with patch("sys.argv", ["reader_gc_graph.py", "--output", "/tmp/gc-no-retry.json"]):
                self.assertEqual(gc.main(), 1)
        build.assert_called_once()
        sleep.assert_not_called()
