import base64
import os
import unittest
from unittest.mock import patch

from scripts import report_ocr_patch_conflicts, review_ocr_patch_conflicts


class OcrPatchConflictTests(unittest.TestCase):
    def conflict(self):
        return {
            "archive_id": 3,
            "repository": "banned-historical-archives3",
            "old_ocr_cache": "old-cache",
            "new_ocr_cache": "new-cache",
            "mirror_ocr_patch": "fork-patch",
            "upstream_ocr_patch": "upstream-patch",
            "articles": [{
                "path": "[article][publication].ts",
                "article_id": "article",
                "publication_id": "publication",
                "doc_id": "3:7:article:publication",
                "new_ocr": {"title": "冲突文章", "content": "新版OCR正文"},
                "patched_candidate": {"title": "冲突文章", "content": "候选校订正文"},
            }],
        }

    def test_issue_renders_three_way_review(self):
        with patch.object(report_ocr_patch_conflicts, "preview", return_value={
            "title": "冲突文章", "content": "当前校订正文",
        }):
            body = report_ocr_patch_conflicts.render_issue(self.conflict())
        self.assertIn("<!-- proofreading-ocr-rebase:3 -->", body)
        self.assertIn("当前校订版 -> 新版 OCR", body)
        self.assertIn("新版 OCR -> 候选版", body)
        self.assertIn("候选校订正文", body)
        self.assertIn("/ocr-keep 1 3", body)
        self.assertIn("?preview=3%3A7%3Aarticle%3Apublication", body)

    def test_markers_round_trip_unicode(self):
        value = {"archive_id": 3, "articles": [{"path": "[文章][来源].ts"}]}
        body = f"<!-- proofreading-ocr-conflict:{report_ocr_patch_conflicts.encode_marker(value)} -->"
        self.assertEqual(
            report_ocr_patch_conflicts.decode_marker(report_ocr_patch_conflicts.CONFLICT_RE, body),
            value,
        )

    def test_upsert_updates_existing_archive_issue(self):
        issues = [{"number": 9, "body": "<!-- proofreading-ocr-rebase:3 -->"}]
        with patch.object(report_ocr_patch_conflicts, "ensure_tracker_label"), \
                patch.object(report_ocr_patch_conflicts, "preview", return_value={}), \
                patch.object(report_ocr_patch_conflicts, "response_or_fail") as request_api:
            report_ocr_patch_conflicts.upsert_conflicts("token", [self.conflict()], issues)
        self.assertEqual(request_api.call_args.args[1], "PATCH")
        self.assertIn("/issues/9", request_api.call_args.args[2])

    def test_successful_rebase_closes_only_selected_issue(self):
        issues = [
            {"number": 3, "body": "<!-- proofreading-ocr-rebase:3 -->"},
            {"number": 4, "body": "<!-- proofreading-ocr-rebase:4 -->"},
        ]
        with patch.dict(os.environ, {"ARCHIVE_ID": "3"}), \
                patch.object(report_ocr_patch_conflicts, "response_or_fail") as request_api:
            report_ocr_patch_conflicts.close_resolved("token", issues)
        request_api.assert_called_once()
        self.assertIn("/issues/3", request_api.call_args.args[2])

    def test_review_selects_multiple_numbers_or_all(self):
        self.assertEqual(review_ocr_patch_conflicts.selected_indices("3, 1 3", 3), [1, 3])
        self.assertEqual(review_ocr_patch_conflicts.selected_indices("all", 3), [1, 2, 3])
        with self.assertRaisesRegex(RuntimeError, "between 1 and 3"):
            review_ocr_patch_conflicts.selected_indices("4", 3)

    def test_review_updates_visible_and_machine_state(self):
        conflict = {"archive_id": 3, "articles": self.conflict()["articles"]}
        original = "\n".join([
            f"<!-- proofreading-ocr-decisions:{report_ocr_patch_conflicts.encode_marker({})} -->",
            "<!-- ocr-review-start -->", "old", "<!-- ocr-review-end -->",
        ])
        updated = review_ocr_patch_conflicts.update_issue_body(
            original, conflict, {"[article][publication].ts": "keep"},
        )
        decisions = report_ocr_patch_conflicts.decode_marker(report_ocr_patch_conflicts.DECISIONS_RE, updated)
        self.assertEqual(decisions, {"[article][publication].ts": "keep"})
        self.assertIn("[x] 1.", updated)
        self.assertIn("保留补丁", updated)

    def test_drop_resets_local_patch_file_to_upstream(self):
        article = self.conflict()["articles"][0]
        with patch.object(review_ocr_patch_conflicts, "repository_file", side_effect=[
                    ("local", "local-sha"), ("upstream", "upstream-sha"),
                ]), \
                patch.object(review_ocr_patch_conflicts, "response_or_fail") as request_api:
            result = review_ocr_patch_conflicts.drop_local_patch("token", self.conflict(), article)
        self.assertEqual(result, "reset to upstream")
        self.assertEqual(request_api.call_args.args[1], "PUT")
        payload = request_api.call_args.args[4]
        self.assertEqual(base64.b64decode(payload["content"]).decode(), "upstream")

    def test_completed_review_dispatches_archive_rebuild(self):
        with patch.object(review_ocr_patch_conflicts, "response_or_fail") as request_api:
            review_ocr_patch_conflicts.dispatch_rebuild("token", 3, 9)
        payload = request_api.call_args.args[4]
        self.assertEqual(payload["event_type"], "ocr-rebase-reviewed")
        self.assertEqual(payload["client_payload"]["archive_id"], "3")
        self.assertEqual(payload["client_payload"]["allow_ocr_patch_rebase"], "true")


if __name__ == "__main__":
    unittest.main()
