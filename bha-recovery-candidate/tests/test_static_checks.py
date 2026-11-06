"""Strict gates exercise actual source and reject injected type errors."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StaticChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bha-static-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("pyrightconfig.json", "jsconfig.service-worker.json"):
            shutil.copyfile(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "app", self.root / "app", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "typings", self.root / "typings")
        (self.root / "static").mkdir()
        shutil.copyfile(ROOT / "static/sw.js", self.root / "static/sw.js")
        (self.root / "node_modules").symlink_to(ROOT / "node_modules", target_is_directory=True)

    def check(self, *command):
        return subprocess.run(command, cwd=self.root, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)

    def test_strict_options_and_real_entry_injections(self):
        config = json.loads((self.root / "pyrightconfig.json").read_text())
        jsconfig = json.loads((self.root / "jsconfig.service-worker.json").read_text())
        self.assertEqual(config["typeCheckingMode"], "strict")
        self.assertTrue(jsconfig["compilerOptions"]["strict"])
        self.assertTrue(jsconfig["compilerOptions"]["checkJs"])
        pyright = ("node", str(ROOT / "node_modules/pyright/index.js"), "-p", "pyrightconfig.json")
        checks = [(name, '\n_bad_static_check: int = "wrong"\n', pyright) for name in config["include"]]
        checks.append(("static/sw.js", '\n/** @type {number} */ const badStaticCheck = "wrong";\n',
                       ("node", str(ROOT / "node_modules/typescript/bin/tsc"), "-p", "jsconfig.service-worker.json")))
        for filename, fault, command in checks:
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_text()
                path.write_text(original + fault)
                injected = self.check(*command)
                self.assertNotEqual(injected.returncode, 0, injected.stdout)
                self.assertIn("error", injected.stdout.lower())
                path.write_text(original)
        self.assertEqual((result := self.check(*pyright)).returncode, 0, result.stdout)

    def test_release_image_depends_on_both_strict_checks(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        dockerignore = (ROOT / ".dockerignore").read_text()
        self.assertIn("tsc --project jsconfig.service-worker.json", dockerfile)
        self.assertIn("pyright/index.js --project pyrightconfig.json", dockerfile)
        self.assertIn("COPY --from=static-check-python", dockerfile)
        self.assertIn("unittest discover -s tests -p test_static_checks.py", dockerfile)
        self.assertIn("node tests/test_service_worker.cjs", dockerfile)
        self.assertIn("python -m coverage report --fail-under=80", dockerfile)
        self.assertIn("test_mutation_runner.py", dockerfile)
        for suite in ("tests.test_full_build", "tests.test_parse_boundaries", "tests.test_proofread_boundaries", "tests.test_reindex_scheduler"):
            self.assertIn(suite, dockerfile)
        self.assertIn("test_speech_static.py", dockerfile)
        self.assertIn("COPY jsconfig.speech.json jsconfig.audit.json", dockerfile)
        self.assertIn("tsc --project jsconfig.audit.json", dockerfile)
        self.assertIn("COPY --from=static-check-node /check/package.json", dockerfile)
        self.assertIn("node tests/test_speech_plan.cjs", dockerfile)
        self.assertIn("test_date_static.py", dockerfile)
        self.assertIn("node tests/test_date_terms.cjs", dockerfile)
        for name in (".dockerignore", "package.json", "package-lock.json", "pyrightconfig.json", "jsconfig.service-worker.json", "jsconfig.speech.json", "jsconfig.audit.json",
                     "requirements-coverage.txt", ".coveragerc", "tests/**"):
            self.assertIn("!" + name, dockerignore)
        self.assertIn("!typings/", dockerignore)

    def test_entire_app_and_speech_are_strict_and_reject_real_errors(self):
        config = json.loads((ROOT / "jsconfig.audit.json").read_text(encoding="utf-8"))
        self.assertTrue(config["compilerOptions"]["strict"])
        self.assertTrue(config["compilerOptions"]["checkJs"])
        self.assertTrue(config["compilerOptions"]["noEmit"])
        self.assertFalse(config["compilerOptions"].get("skipLibCheck", False))
        self.assertEqual(set(config["files"]), {"typings/pdfjs-cdn.d.ts", "static/app.js", "static/tts-client.js"})
        self.assertIn("tsc --project jsconfig.audit.json", json.loads((ROOT / "package.json").read_text())["scripts"]["types"])
        for name in config["files"]:
            path = ROOT / name
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            source = path.read_text(encoding="utf-8")
            for directive in ("@ts-ignore", "@ts-nocheck", "@ts-expect-error"):
                self.assertNotIn(directive, source, name)
        shutil.copyfile(ROOT / "jsconfig.audit.json", self.root / "jsconfig.audit.json")
        command = ("node", str(ROOT / "node_modules/typescript/bin/tsc"), "-p", "jsconfig.audit.json", "--pretty", "false")
        baseline = self.check(*command)
        self.assertEqual(baseline.returncode, 0, baseline.stdout)
        entry = self.root / "static/app.js"
        source = entry.read_text(encoding="utf-8")
        faults = (
            ("function collectParseArticles(form) {", 'function collectParseArticles(form) {\n  return 42;'),
            ("function restoreProofreadDraft(item, form) {", 'function restoreProofreadDraft(item, form) {\n  item.doc_id = 42;'),
            ("function proofreadPayload(item, form) {", 'function proofreadPayload(item, form) {\n  return 42;'),
            ("pdfjs.getDocument({", "pdfjs.getDocument({\n      rangeChunkSize: 'wrong',"),
        )
        for before, after in faults:
            with self.subTest(function=before):
                self.assertEqual(source.count(before), 1)
                entry.write_text(source.replace(before, after, 1), encoding="utf-8")
                result = self.check(*command)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("error TS", result.stdout)

    def test_typed_index_mapping_rejects_real_shape_error(self):
        path = self.root / "app/indexer.py"
        original = path.read_text()
        before = '"max_result_window": MAX_RESULT_WINDOW,'
        self.assertEqual(original.count(before), 1)
        path.write_text(original.replace(before, '"max_result_window": "invalid",', 1))
        result = self.check("node", str(ROOT / "node_modules/pyright/index.js"), "-p", "pyrightconfig.json")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not assignable to", result.stdout)
        self.assertIn("int", result.stdout)


if __name__ == "__main__":
    unittest.main()
