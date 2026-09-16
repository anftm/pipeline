import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_reader_assets_plan


class PrepareReaderAssetsPlanTests(unittest.TestCase):
    def test_prepare_splits_only_the_selected_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "queue.json"
            output.write_text(json.dumps({
                "items": [
                    {"extension": "pdf", "key": "a"},
                    {"extension": "docx", "key": "b"},
                ],
                "stale_keys": ["old"],
                "authoritative_snapshot": True,
            }))
            with patch.object(prepare_reader_assets_plan, "scan_reader_assets") as scanner:
                scanner.shard_for_key.return_value = 0
                with patch.dict(os.environ, {"FORCE_REBUILD": "true", "INPUT_PATH": ""}, clear=False):
                    extension, count, stale, authoritative = prepare_reader_assets_plan.prepare(output)
            self.assertEqual((extension, count, stale, authoritative), ("pdf", 1, 1, True))
            self.assertEqual(json.loads((root / "queue.json").read_text())["items"], [{"extension": "pdf", "key": "a"}])


if __name__ == "__main__":
    unittest.main()
