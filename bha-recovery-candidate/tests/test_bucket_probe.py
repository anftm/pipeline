import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import bucket_probe


class BucketSnapshotProbeTests(unittest.TestCase):
    def test_creates_snapshots_and_restores_isolated_index(self):
        client = MagicMock()
        client.count.return_value = {"count": 1}
        original_argv = sys.argv
        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(bucket_probe, "Elasticsearch", return_value=client), \
                    patch.object(bucket_probe, "LOCAL_REPOSITORY", Path(directory) / "local-repository"):
                sys.argv = ["bucket_probe", directory]
                self.assertEqual(bucket_probe.main(), 0)
        finally:
            sys.argv = original_argv

        created_index = client.indices.create.call_args.kwargs["index"]
        self.assertTrue(created_index.startswith("bha-bucket-probe-"))
        self.assertEqual(client.snapshot.create_repository.call_args.kwargs["repository"]["type"], "fs")
        self.assertTrue(client.snapshot.create_repository.call_args.kwargs["repository"]["settings"]["location"].endswith("local-repository"))
        self.assertEqual(client.snapshot.create.call_args.kwargs["indices"], created_index)
        self.assertEqual(client.snapshot.restore.call_args.kwargs["indices"], created_index)
        client.indices.delete.assert_called_with(index=created_index, ignore_unavailable=True)


if __name__ == "__main__":
    unittest.main()
