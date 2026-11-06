"""Do not silently discard unimported application modules from coverage."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CoverageGateTests(unittest.TestCase):
    def test_unimported_application_module_is_reported_uncovered(self):
        with tempfile.TemporaryDirectory(prefix="bha-coverage-") as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / ".coveragerc", root / ".coveragerc")
            (root / "app").mkdir()
            (root / "app/__init__.py").write_text("value = 1\n")
            (root / "app/missing.py").write_text("value = 2\n")
            (root / "app/__main__.py").write_text("value = 3\n")
            environment = {**os.environ, "COVERAGE_FILE": str(root / ".coverage"),
                           "COVERAGE_RCFILE": str(root / ".coveragerc")}
            for command in ((sys.executable, "-m", "coverage", "run", "-m", "app"),
                            (sys.executable, "-m", "coverage", "json", "-o", "coverage.json")):
                result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            thresholds = subprocess.run((sys.executable, "-m", "coverage", "report", "--fail-under=100"),
                                        cwd=root, env=environment, text=True, capture_output=True, timeout=30)
            self.assertNotEqual(thresholds.returncode, 0, thresholds.stdout)
            report = json.loads((root / "coverage.json").read_text())
            self.assertIn("app/missing.py", report["files"])
            self.assertEqual(report["files"]["app/missing.py"]["summary"]["covered_lines"], 0)

    def test_production_sources_include_entire_application(self):
        # The repository is a namespace package; keep the fixture in that
        # shape so coverage discovery is tested against the real layout.
        with tempfile.TemporaryDirectory(prefix="bha-coverage-sources-") as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / ".coveragerc", root / ".coveragerc")
            source = root / "app"
            source.mkdir()
            for path in (ROOT / "app").glob("*.py"):
                shutil.copyfile(path, source / path.name)
            (root / "probe.py").write_text("from app import config\n")
            environment = {**os.environ, "COVERAGE_FILE": str(root / ".coverage"),
                           "COVERAGE_RCFILE": str(root / ".coveragerc")}
            for command in ((sys.executable, "-m", "coverage", "run", "probe.py"),
                            (sys.executable, "-m", "coverage", "json", "-o", "coverage.json")):
                result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((root / "coverage.json").read_text())
            expected = {f"app/{path.name}" for path in (ROOT / "app").glob("*.py")}
            self.assertTrue(expected.issubset(report["files"]), expected - set(report["files"]))


if __name__ == "__main__":
    unittest.main()
