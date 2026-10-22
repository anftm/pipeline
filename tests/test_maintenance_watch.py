from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import maintenance_watch as watch


ROOT = Path(__file__).resolve().parents[1]


class MaintenanceWatchTests(unittest.TestCase):
    def test_parse_requirements_reads_pins_and_skips_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.txt"
            path.write_text(
                "bleach[css]==6.4.0\n"
                "lxml==6.1.3\n"
                "-r other.txt\n"
                "requests>=2\n"
                "# comment\n",
                encoding="utf-8",
            )
            self.assertEqual(
                watch.parse_requirements(path),
                [("bleach", "6.4.0"), ("lxml", "6.1.3")],
            )

    def test_baseline_reports_outdated_pins(self):
        current = {
            "dependencies": {
                "lxml": {
                    "name": "lxml",
                    "pinned": "6.1.3",
                    "latest": "6.2.0",
                    "files": ["scripts/requirements.txt"],
                }
            },
            "datasets": {},
            "github": {},
        }
        changes, baseline = watch.detect_changes({}, current)
        self.assertTrue(baseline)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["name"], "lxml")
        self.assertEqual(changes[0]["old"], "6.1.3")
        self.assertEqual(changes[0]["new"], "6.2.0")
        self.assertTrue(changes[0]["baseline"])

    def test_detects_new_latest_version_and_dataset_revision(self):
        previous = {
            "dependencies": {"lxml": {"latest": "6.1.3"}},
            "datasets": {"VoiceOfML/Teachers": {"revision": "old"}},
            "github": {},
        }
        current = {
            "dependencies": {
                "lxml": {
                    "name": "lxml",
                    "pinned": "6.1.3",
                    "latest": "6.2.0",
                    "files": ["scripts/requirements.txt"],
                }
            },
            "datasets": {
                "VoiceOfML/Teachers": {
                    "revision": "new",
                    "url": "https://huggingface.co/datasets/VoiceOfML/Teachers",
                }
            },
            "github": {},
        }
        changes, baseline = watch.detect_changes(previous, current)
        self.assertFalse(baseline)
        self.assertEqual({change["type"] for change in changes}, {"dependency", "dataset"})
        self.assertEqual(
            {change["name"] for change in changes},
            {"lxml", "VoiceOfML/Teachers"},
        )

    def test_manual_check_is_due_and_ack_resets_the_timer(self):
        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        current = {
            "checked_at": now.isoformat(),
            "dependencies": {},
            "datasets": {},
            "http_pages": {},
            "manual_checks": {
                "百度网盘数据集": {
                    "url": "https://pan.baidu.com/s/example",
                    "note": "人工确认",
                    "interval_days": 30,
                }
            },
            "github": {},
        }
        changes, baseline = watch.detect_changes({}, current, now=now)
        self.assertTrue(baseline)
        self.assertEqual([change["type"] for change in changes], ["manual"])
        state = watch.prepare_state(current, {}, changes, now=now)
        self.assertEqual(state["manual_checks"]["百度网盘数据集"]["last_reminded"], now.isoformat())
        self.assertNotIn("url", state["manual_checks"]["百度网盘数据集"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            code = watch.acknowledge(
                {"manual_checks": [{"name": "百度网盘数据集", "url": "https://pan.baidu.com/s/example"}]},
                path,
                ["百度网盘数据集"],
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertNotIn("url", saved["manual_checks"]["百度网盘数据集"])
        confirmed = saved["manual_checks"]["百度网盘数据集"]["last_confirmed"]
        self.assertTrue(confirmed)
        self.assertEqual(
            confirmed,
            saved["manual_checks"]["百度网盘数据集"]["last_reminded"],
        )

    def test_manual_url_can_come_from_environment(self):
        with patch.dict(os.environ, {"BAIDU_UPSTREAM_1_URL": "https://pan.baidu.com/s/example"}):
            records, errors = watch.collect_manual_checks({
                "manual_checks": [{
                    "name": "百度网盘上游 1",
                    "url_env": "BAIDU_UPSTREAM_1_URL",
                    "interval_days": 30,
                }]
            })
        self.assertEqual(errors, [])
        self.assertEqual(records["百度网盘上游 1"]["url"], "https://pan.baidu.com/s/example")

    def test_http_page_fingerprint_regex(self):
        with patch.object(
            watch, "http_body",
            return_value=("最后更新：2026-10-22".encode("utf-8"), "Wed, 22 Oct 2026 00:00:00 GMT"),
        ):
            records, errors = watch.collect_http_pages({
                "http_pages": [{
                    "name": "毛泽东博览更新日志",
                    "url": "https://example.test/update",
                    "fingerprint_regex": r"最后更新：([0-9-]+)",
                }]
            })
        self.assertEqual(errors, [])
        self.assertEqual(records["毛泽东博览更新日志"]["fingerprint"], "2026-10-22")

    def test_no_change_is_empty(self):
        current = {
            "dependencies": {"lxml": {"latest": "6.2.0"}},
            "datasets": {"VoiceOfML/Teachers": {"revision": "same"}},
            "github": {},
        }
        previous = {
            "dependencies": {"lxml": {"latest": "6.2.0"}},
            "datasets": {"VoiceOfML/Teachers": {"revision": "same"}},
            "github": {},
        }
        self.assertEqual(watch.detect_changes(previous, current), ([], False))

    def test_message_includes_names_versions_and_links(self):
        current = {"checked_at": "2026-10-22T00:00:00+00:00", "datasets": {}, "github": {}}
        changes = [{
            "type": "dependency",
            "name": "lxml",
            "old": "6.1.3",
            "new": "6.2.0",
            "pinned": "6.1.3",
            "files": ["scripts/requirements.txt"],
            "url": "https://pypi.org/project/lxml/",
        }]
        subject, body = watch.build_message(current, changes, [], False)
        self.assertIn("1 项", subject)
        self.assertIn("lxml", body)
        self.assertIn("6.2.0", body)
        self.assertIn("https://pypi.org/project/lxml/", body)

    def test_config_and_workflow_contract(self):
        config = json.loads((ROOT / "scripts/maintenance-watch.json").read_text(encoding="utf-8"))
        self.assertIn("scripts/requirements.txt", config["requirements"])
        self.assertIn("VoiceOfML/Teachers", config["hf_datasets"])
        self.assertEqual(len(config["manual_checks"]), 2)
        self.assertEqual(
            {entry.get("url_env") for entry in config["manual_checks"]},
            {"BAIDU_UPSTREAM_1_URL", "BAIDU_UPSTREAM_2_URL"},
        )
        self.assertNotIn("pan.baidu.com", json.dumps(config, ensure_ascii=False))
        workflow = (ROOT / ".github/workflows/ops-maintenance-watch.yml").read_text(encoding="utf-8")
        self.assertIn("cron: '17 1 * * *'", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("SMTP_PASSWORD", workflow)
        self.assertIn("BAIDU_UPSTREAM_1_URL", workflow)
        self.assertIn("tests.test_maintenance_watch", workflow)
        self.assertIn("state/maintenance-watch.json", workflow)
        self.assertIn("ack", workflow)
        self.assertNotIn("reader-sidecar", workflow)

    def test_send_email_requires_smtp_configuration(self):
        with patch.dict(watch.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                watch.send_email("subject", "body")


if __name__ == "__main__":
    unittest.main()
