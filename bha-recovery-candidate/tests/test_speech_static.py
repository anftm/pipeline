"""Check the complete production speech client, including its controller."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "// BEGIN STRICT SPEECH PLANNER"
END = "// END STRICT SPEECH PLANNER"


class SpeechPlanStaticTests(unittest.TestCase):
    def test_full_speech_client_is_strict_and_rejects_real_errors(self):
        source = (ROOT / "static/tts-client.js").read_text(encoding="utf-8")
        self.assertEqual(source.count(BEGIN), 1)
        self.assertEqual(source.count(END), 1)
        self.assertNotIn("@ts-ignore", source)
        self.assertNotIn("@ts-nocheck", source)
        config = json.loads((ROOT / "jsconfig.speech.json").read_text(encoding="utf-8"))
        self.assertTrue(config["compilerOptions"]["strict"])
        self.assertTrue(config["compilerOptions"]["checkJs"])
        self.assertEqual(config["files"], ["static/tts-client.js"])
        with tempfile.TemporaryDirectory(prefix="speech-typecheck-") as directory:
            root = Path(directory)
            (root / "static").mkdir()
            entry = root / "static/tts-client.js"
            entry.write_text(source, encoding="utf-8")
            shutil.copyfile(ROOT / "jsconfig.speech.json", root / "jsconfig.speech.json")
            cmd = ("node", str(ROOT / "node_modules/typescript/bin/tsc"), "-p", str(root / "jsconfig.speech.json"), "--pretty", "false")
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            faults = (("function safe(text, offset) {", 'function safe(text, offset) {\n    offset = "wrong";'),
                      ("function advance(value) {", 'function advance(value) {\n    value.part = "wrong";'))
            for before, after in faults:
                with self.subTest(function=before):
                    self.assertEqual(source.count(before), 1)
                    entry.write_text(source.replace(before, after, 1), encoding="utf-8")
                    result = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("error TS", result.stdout)


if __name__ == "__main__":
    unittest.main()
