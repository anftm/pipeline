import unittest
from unittest.mock import Mock, patch

from scripts import reader_gc_graph as gc


class ReaderGcRetryTests(unittest.TestCase):
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
