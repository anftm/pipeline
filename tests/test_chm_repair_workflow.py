import unittest
from pathlib import Path

import yaml


class ChmRepairWorkflowTests(unittest.TestCase):
    def test_chm_rebuild_uses_canonical_index_and_serializes_unification(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/reader-chm-repair.yml").read_text())
        self.assertEqual(workflow["concurrency"]["group"], "reader-sidecar")
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertFalse(inputs["apply"]["default"])
        self.assertEqual(inputs["limit"]["default"], "100")
        self.assertFalse(inputs["retry_failed"]["default"])
        self.assertFalse(inputs["failed_only"]["default"])
        steps = workflow["jobs"]["plan"]["steps"]
        plan_step = next(step for step in steps if step.get("name") == "Plan only sources on the old CHM chapter profile")
        fetch_step = next(step for step in steps if step.get("name") == "Fetch pinned CHM source metadata")
        self.assertEqual(fetch_step["env"]["SKIP_SIZES"], "true")
        self.assertEqual(plan_step["id"], "plan")
        plan = plan_step["run"]
        self.assertIn("--extension chm", plan)
        self.assertIn("--plan-only", plan)
        self.assertIn("--retry-failures", plan)
        self.assertIn("--failed-only", plan)
        self.assertIn("output/chm-plan.json", "\n".join(step.get("run", "") for step in steps))
        rebuild = workflow["jobs"]["rebuild"]
        commands = "\n".join(step.get("run", "") for step in rebuild["steps"])
        self.assertIn("--apply --workers 2", commands)
        self.assertIn("build_reader_assets_v2_sidecar.py --apply", commands)
        self.assertIn("reader-v3-publish.yml", commands)
        self.assertIn("operation=unify", commands)


if __name__ == "__main__":
    unittest.main()
