import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import monthly_reminder as reminder


ROOT = Path(__file__).resolve().parents[1]


class MonthlyReminderTests(unittest.TestCase):
    def test_message_contains_all_four_checklists(self):
        subject, body = reminder.build_message(datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertIn("2026-11", subject)
        self.assertIn("中马库", body)
        self.assertIn("百度网盘上游", body)
        self.assertIn("毛泽东博览", body)
        self.assertIn("依赖", body)
        self.assertIn("pipeline.update --run", body)

    def test_links_come_from_environment_and_missing_secrets_are_visible(self):
        with patch.dict(reminder.os.environ, {
            "BAIDU_UPSTREAM_1_URL": "https://example.test/baidu-1",
            "BAIDU_UPSTREAM_2_URL": "https://example.test/baidu-2",
            "MAOZEDONG_UPDATE_URL": "https://example.test/mao-update",
        }, clear=True):
            _subject, body = reminder.build_message()
        self.assertIn("https://example.test/baidu-1", body)
        self.assertIn("https://example.test/baidu-2", body)
        self.assertIn("https://example.test/mao-update", body)

        with patch.dict(reminder.os.environ, {}, clear=True):
            _subject, body = reminder.build_message()
        self.assertIn("未配置 BAIDU_UPSTREAM_1_URL", body)
        self.assertIn("未配置 BAIDU_UPSTREAM_2_URL", body)
        self.assertIn("未配置 MAOZEDONG_UPDATE_URL", body)

    def test_dry_run_does_not_need_smtp(self):
        with patch.dict(reminder.os.environ, {}, clear=True):
            self.assertEqual(reminder.main(["--dry-run"]), 0)

    def test_send_email_requires_smtp_configuration(self):
        with patch.dict(reminder.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                reminder.send_email("subject", "body")

    def test_workflow_is_monthly_read_only_and_has_required_secrets(self):
        workflow = (ROOT / ".github/workflows/ops-monthly-reminder.yml").read_text(encoding="utf-8")
        self.assertIn("cron: '0 1 1 * *'", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("SMTP_PASSWORD", workflow)
        self.assertIn("BAIDU_UPSTREAM_1_URL", workflow)
        self.assertIn("MAOZEDONG_UPDATE_URL", workflow)
        self.assertIn("tests.test_monthly_reminder", workflow)
        self.assertIn("monthly_reminder.py", workflow)
        self.assertNotIn("state/", workflow)


if __name__ == "__main__":
    unittest.main()
