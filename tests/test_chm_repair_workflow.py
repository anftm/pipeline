import unittest
from pathlib import Path

import yaml


class ChmRepairWorkflowTests(unittest.TestCase):
    def test_chm_rebuild_is_explicit_bounded_and_serializes_publication(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/repair-chm-conversions.yml").read_text())
        self.assertEqual(workflow["concurrency"]["group"], "reader-sidecar")
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertFalse(inputs["apply"]["default"])
        self.assertEqual(inputs["limit"]["default"], "100")
        steps = workflow["jobs"]["plan"]["steps"]
        scan = next(step["run"] for step in steps if step.get("name") == "Plan stale CHM profiles")
        self.assertIn("--extension chm", scan)
        self.assertIn("--limit", scan)
        convert = workflow["jobs"]["convert"]
        self.assertEqual(convert["strategy"]["max-parallel"], 3)
        self.assertIn("validate all mapped source text", convert["steps"][-2]["name"])
        publish = workflow["jobs"]["publish"]
        self.assertNotIn("strategy", publish)
        self.assertIn("--bundles", publish["steps"][-1]["run"])
        self.assertIn("reader-v3-publish.yml", workflow["jobs"]["finalize"]["steps"][0]["run"])
        self.assertIn("operation=unify", workflow["jobs"]["finalize"]["steps"][0]["run"])


if __name__ == "__main__":
    unittest.main()
