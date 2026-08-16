import io
import base64
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import fork_repositories, publish_proofread_upstream, report_ocr_patch_conflicts, review_ocr_patch_conflicts, review_proofread, submit_proofread, update_proofread_issues


class ProofreadBundleTests(unittest.TestCase):
    def test_patch_path_targets_archive_repository_root(self):
        self.assertEqual(submit_proofread.patch_path(0, "article", "book"), "[article][book].ts")

    def test_ocr_conflict_issue_renders_article_preview(self):
        conflict = {
            "archive_id": 3,
            "repository": "banned-historical-archives3",
            "old_ocr_cache": "old-cache",
            "new_ocr_cache": "new-cache",
            "mirror_ocr_patch": "fork-patch",
            "upstream_ocr_patch": "upstream-patch",
            "articles": [{
                "path": "[article][publication].ts", "article_id": "article",
                "publication_id": "publication", "doc_id": "3:7:article:publication",
                "new_ocr": {"title": "冲突文章", "content": "新版OCR正文"},
                "patched_candidate": {"title": "冲突文章", "content": "候选校订正文"},
            }],
        }
        with patch.object(report_ocr_patch_conflicts, "preview", return_value={
            "title": "冲突文章", "content": "当前校订正文",
        }):
            body = report_ocr_patch_conflicts.render_issue(conflict)
        self.assertIn("<!-- proofreading-ocr-rebase:3 -->", body)
        self.assertIn("冲突文章", body)
        self.assertIn("当前校订正文", body)
        self.assertIn("当前校订版 -> 新版 OCR", body)
        self.assertIn("新版 OCR -> 候选版", body)
        self.assertIn("候选校订正文", body)
        self.assertIn("?preview=3%3A7%3Aarticle%3Apublication", body)
        self.assertIn("[article][publication].ts", body)

    def test_ocr_conflict_diff_reports_identical_text(self):
        self.assertEqual(
            report_ocr_patch_conflicts.text_diff("相同", "相同", "旧", "新"),
            "无文本差异。",
        )

    def test_ocr_conflict_markers_round_trip_unicode(self):
        value = {"archive_id": 3, "articles": [{"path": "[文章][来源].ts"}]}
        body = f"<!-- proofreading-ocr-conflict:{report_ocr_patch_conflicts.encode_marker(value)} -->"
        self.assertEqual(
            report_ocr_patch_conflicts.decode_marker(report_ocr_patch_conflicts.CONFLICT_RE, body),
            value,
        )

    def test_ocr_review_selects_multiple_numbers_or_all(self):
        self.assertEqual(review_ocr_patch_conflicts.selected_indices("3, 1 3", 3), [1, 3])
        self.assertEqual(review_ocr_patch_conflicts.selected_indices("all", 3), [1, 2, 3])
        with self.assertRaisesRegex(RuntimeError, "between 1 and 3"):
            review_ocr_patch_conflicts.selected_indices("4", 3)

    def test_ocr_review_updates_visible_and_machine_state(self):
        conflict = {
            "archive_id": 3,
            "articles": [{"path": "[article][publication].ts", "article_id": "article"}],
        }
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

    def test_ocr_drop_resets_local_patch_file_to_upstream(self):
        conflict = {
            "repository": "banned-historical-archives3",
            "upstream_ocr_patch": "upstream-sha",
        }
        article = {"path": "[article][publication].ts", "article_id": "article"}
        with patch.object(review_ocr_patch_conflicts, "repository_file", side_effect=[
                    ("local", "local-sha"), ("upstream", "upstream-sha"),
                ]), \
                patch.object(review_ocr_patch_conflicts, "response_or_fail") as request_api:
            result = review_ocr_patch_conflicts.drop_local_patch("token", conflict, article)
        self.assertEqual(result, "reset to upstream")
        self.assertEqual(request_api.call_args.args[1], "PUT")
        payload = request_api.call_args.args[4]
        self.assertEqual(base64.b64decode(payload["content"]).decode(), "upstream")
        self.assertEqual(payload["sha"], "local-sha")

    def test_completed_ocr_review_dispatches_archive_rebuild(self):
        with patch.object(review_ocr_patch_conflicts, "response_or_fail") as request_api:
            review_ocr_patch_conflicts.dispatch_rebuild("token", 3, 9)
        payload = request_api.call_args.args[4]
        self.assertEqual(payload["event_type"], "ocr-rebase-reviewed")
        self.assertEqual(payload["client_payload"]["archive_id"], "3")
        self.assertEqual(payload["client_payload"]["allow_ocr_patch_rebase"], "true")

    def test_ocr_conflict_upsert_updates_existing_archive_issue(self):
        conflict = {
            "archive_id": 3, "repository": "repo", "old_ocr_cache": "old",
            "new_ocr_cache": "new", "mirror_ocr_patch": "fork",
            "upstream_ocr_patch": "upstream", "articles": [],
        }
        issues = [{"number": 9, "body": "<!-- proofreading-ocr-rebase:3 -->"}]
        with patch.object(report_ocr_patch_conflicts, "ensure_tracker_label"), \
                patch.object(report_ocr_patch_conflicts, "response_or_fail") as request_api:
            report_ocr_patch_conflicts.upsert_conflicts("token", [conflict], issues)
        self.assertEqual(request_api.call_args.args[1], "PATCH")
        self.assertIn("/issues/9", request_api.call_args.args[2])

    def test_successful_rebase_closes_selected_conflict_issue(self):
        issues = [
            {"number": 3, "body": "<!-- proofreading-ocr-rebase:3 -->"},
            {"number": 4, "body": "<!-- proofreading-ocr-rebase:4 -->"},
        ]
        with patch.dict(os.environ, {"ARCHIVE_ID": "3"}), \
                patch.object(report_ocr_patch_conflicts, "response_or_fail") as request_api:
            report_ocr_patch_conflicts.close_resolved("token", issues)
        request_api.assert_called_once()
        self.assertIn("/issues/3", request_api.call_args.args[2])
        self.assertEqual(request_api.call_args.args[4], {"state": "closed", "state_reason": "completed"})

    def test_manual_review_creates_tracker_issue(self):
        request = {
            "title": "校订测试文章", "doc_id": "doc-1",
            "metadata": {"article": {"tags": [{"name": "标签", "type": "主题/事件"}]}},
        }
        pulls = [{"number": 5, "url": "https://example.test/pr/5", "sha": "a" * 40}]
        created = {"number": 9, "html_url": "https://example.test/issues/9"}
        with patch.object(submit_proofread, "ensure_tracker_label"), \
                patch.object(submit_proofread, "find_tracker_issue", return_value=None), \
                patch.object(submit_proofread, "response_or_fail", return_value=created) as request_api:
            url = submit_proofread.upsert_tracker_issue(
                "token", "correction", request, "banned-historical-archives3", "article-1", pulls,
            )
        self.assertEqual(url, created["html_url"])
        payload = request_api.call_args.args[4]
        self.assertNotIn("assignees", payload)
        self.assertEqual(payload["labels"], ["proofreading-review"])
        self.assertEqual(payload["title"], "校订审核：测试文章")
        self.assertIn("<!-- proofreading:correction -->", payload["body"])
        self.assertIn("banned-historical-archives3#5", payload["body"])
        self.assertIn("- 文章：测试文章", payload["body"])
        self.assertIn("## 审核方式", payload["body"])
        self.assertIn("元数据核对新增、删除或原值与新值", payload["body"])
        self.assertIn("评论 `/approve`（合并本 Issue 关联的全部 PR）", payload["body"])
        self.assertIn("评论 `/reject 原因`（关闭本 Issue 关联的全部 PR，并记录原因）", payload["body"])
        self.assertNotIn("查看下方“修改内容”", payload["body"])

    def test_tracker_issue_title_strips_duplicate_proofreading_prefix(self):
        request = {
            "title": "校订 中央军委转发五十四军对反对派进行工作情况报告的批语",
            "doc_id": "0:10:0000fe895f:maoquanji49",
            "metadata": {"article": {"tags": []}},
        }
        pulls = [{"number": 5, "url": "https://example.test/pr/5", "sha": "a" * 40}]
        created = {"number": 9, "html_url": "https://example.test/issues/9"}
        with patch.object(submit_proofread, "ensure_tracker_label"), \
                patch.object(submit_proofread, "find_tracker_issue", return_value=None), \
                patch.object(submit_proofread, "response_or_fail", return_value=created) as request_api:
            submit_proofread.upsert_tracker_issue(
                "token", "correction", request, "banned-historical-archives0", "0000fe895f", pulls,
            )
        payload = request_api.call_args.args[4]
        self.assertEqual(payload["title"], "校订审核：中央军委转发五十四军对反对派进行工作情况报告的批语")
        self.assertIn("- 文章：中央军委转发五十四军对反对派进行工作情况报告的批语", payload["body"])
        self.assertIn("BHA 预览：https://vomebook-bha-search.hf.space/?preview=0%3A10%3A0000fe895f%3Amaoquanji49", payload["body"])
        self.assertIn("- [ ] [banned-historical-archives0#5](https://example.test/pr/5)", payload["body"])

    def test_tracker_issue_renders_change_details(self):
        request = {
            "title": "校订测试文章", "doc_id": "doc-1",
            "changed": [
                {"kind": "part", "index": 1, "original": "旧文", "edited": "旧新"},
                {"kind": "part", "index": 1, "insert": True, "text": "插入段", "part_type": "paragraph"},
                {"kind": "part", "index": 3, "delete": True, "original": "删除段"},
                {"kind": "comment", "index": 1, "original": "注释", "edited": "注注释"},
                {"kind": "description", "original": "", "edited": "补充说明"},
                {"kind": "metadata", "field": "title", "old": "旧标题", "new": "新标题"},
                {"kind": "metadata", "field": "tags", "old": "[{'name': '旧'}]", "new": "[{'name': '新'}]"},
            ],
            "fulltext": {"original": "第一段\n第二段", "edited": "第一段\n新第二段"},
            "metadata": {"article": {"tags": []}},
        }
        pulls = [{"number": 5, "url": "https://example.test/pr/5", "sha": "a" * 40}]
        created = {"number": 9, "html_url": "https://example.test/issues/9"}
        with patch.object(submit_proofread, "ensure_tracker_label"), \
                patch.object(submit_proofread, "find_tracker_issue", return_value=None), \
                patch.object(submit_proofread, "response_or_fail", return_value=created) as request_api:
            submit_proofread.upsert_tracker_issue(
                "token", "correction", request, "banned-historical-archives0", "0000fe895f", pulls,
            )
        body = request_api.call_args.args[4]["body"]
        self.assertIn("## 修改内容", body)
        for line in ("- 段落 1：旧文 → 旧新", "- 段落 1后插入：插入段", "- 段落 3（删除）：删除段",
                     "- 注释 1：注释 → 注注释", "- 描述： → 补充说明", "- 标题：旧标题 → 新标题",
                     "- 标签：删除 ~~旧~~；新增 **新**"):
            self.assertIn(line, body)
        for text in ("旧文", "旧新", "插入段", "删除段", "注释", "注注释", "补充说明", "旧标题", "新标题"):
            self.assertIn(text, body)
        self.assertNotIn("### 段落 1", body)
        self.assertIn("## 原全文\n\n```text\n第一段\n第二段\n```", body)
        self.assertIn("## 修改后全文\n\n```text\n第一段\n新第二段\n```", body)
        self.assertIn("## 审核方式", body)

    def test_tracker_issue_moves_long_fulltext_to_complete_comments(self):
        original = "旧" * 40000
        edited = "新" * 40000
        request = {
            "title": "长文章", "doc_id": "doc-1",
            "changed": [{"kind": "part", "index": 1, "original": "旧", "edited": "新"}],
            "fulltext": {"original": original, "edited": edited},
        }
        pulls = [{"number": 5, "url": "https://example.test/pr/5"}]
        created = {"number": 9, "html_url": "https://example.test/issues/9"}
        with patch.object(submit_proofread, "ensure_tracker_label"), \
                patch.object(submit_proofread, "find_tracker_issue", return_value=None), \
                patch.object(submit_proofread, "existing_comment_bodies", return_value=[]), \
                patch.object(submit_proofread, "response_or_fail", return_value=created) as request_api:
            submit_proofread.upsert_tracker_issue(
                "token", "correction", request, "banned-historical-archives0", "article", pulls,
            )
        bodies = [call.args[4]["body"] for call in request_api.call_args_list]
        self.assertEqual(len(bodies), 3)
        self.assertLessEqual(max(map(len, bodies)), submit_proofread.GITHUB_BODY_LIMIT)
        self.assertNotIn("## 原全文", bodies[0])
        self.assertIn("后续评论", bodies[0])
        self.assertIn(original, bodies[1])
        self.assertIn(edited, bodies[2])

    def test_oversized_fulltext_comment_chunks_reassemble_without_loss(self):
        original = "旧" * 120001
        edited = "新" * 120001
        bodies = submit_proofread.fulltext_comment_bodies(
            {"fulltext": {"original": original, "edited": edited}}, "proofreading-fulltext:correction",
        )
        self.assertGreater(len(bodies), 2)
        self.assertTrue(all(len(body) <= submit_proofread.GITHUB_BODY_LIMIT for _marker, body in bodies))

        def content(body):
            return body.split("```text\n", 1)[1].rsplit("\n```", 1)[0]

        rebuilt_original = "".join(content(body) for marker, body in bodies if ":original:" in marker)
        rebuilt_edited = "".join(content(body) for marker, body in bodies if ":edited:" in marker)
        self.assertEqual(rebuilt_original, original)
        self.assertEqual(rebuilt_edited, edited)

    def test_change_details_escape_user_markdown_structure(self):
        body = "\n".join(submit_proofread.change_details({"changed": [{
            "kind": "part", "index": 1, "original": "旧", "edited": "新\n\n## 伪造审核\n- [x] 已核对",
        }]}))
        self.assertNotIn("\n## 伪造审核", body)
        self.assertNotIn("\n- [x] 已核对", body)
        self.assertIn("<br><br>\\#\\# 伪造审核", body)

    def test_proofread_pr_body_renders_changes(self):
        request = {
            "title": "校订测试文章", "doc_id": "doc-1", "description": "校对员备注",
            "patch": {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}, "description": ""},
            "changed": [
                {"kind": "part", "index": 1, "original": "旧文", "edited": "新文"},
                {"kind": "metadata", "field": "title", "old": "旧标题", "new": "新标题"},
            ],
            "fulltext": {"original": "旧文全文", "edited": "新文全文"},
        }
        body = submit_proofread.proofread_pr_body(
            request, "banned-historical-archives3", "article-1", "correction",
        )
        self.assertIn("<!-- proofreading:correction -->", body)
        self.assertIn("- Archive：`banned-historical-archives3`", body)
        self.assertIn("- Article ID：`article-1`", body)
        self.assertIn("- 修改：正文段落 1 处、标题", body)
        self.assertIn("## 修改内容", body)
        self.assertIn("- 段落 1：旧文 → 新文", body)
        self.assertIn("旧文", body)
        self.assertIn("新文", body)
        self.assertIn("## 元数据对照", body)
        self.assertIn("| 标题 | 旧标题 | 新标题 |", body)
        self.assertIn("## 原全文", body)
        self.assertIn("## 修改后全文", body)
        self.assertIn("BHA 预览：https://vomebook-bha-search.hf.space/?preview=doc-1", body)
        self.assertIn("- 说明：校对员备注", body)
        self.assertNotIn("## 审核方式", body)

    def test_proofread_pr_body_drops_fulltext_over_github_limit(self):
        huge = "长" * 100000
        request = {
            "title": "超长文章", "doc_id": "doc-1",
            "changed": [{"kind": "part", "index": 1, "original": "旧", "edited": "新"}],
            "fulltext": {"original": huge, "edited": huge + "新"},
        }
        body = submit_proofread.proofread_pr_body(
            request, "banned-historical-archives3", "article-1", "correction",
        )
        self.assertLessEqual(len(body), 60000)
        self.assertNotIn("## 原全文", body)
        self.assertNotIn("## 修改后全文", body)
        self.assertIn("## 修改内容", body)
        self.assertIn("全文对照较长", body)

    def test_submit_flow_uses_human_readable_pull_body(self):
        request = {
            "kind": "proofread", "archive_id": 3, "article_id": "oldid",
            "publication_id": "publication",
            "metadata": {"article": {"title": "新标题"}},
            "patch": {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}, "description": ""},
            "changed": [{"kind": "part", "index": 1, "original": "旧文", "edited": "新文"}],
            "fulltext": {"original": "旧文", "edited": "新文"},
        }
        bodies = []

        def get_file(_token, _repo, branch, path):
            if branch == "config":
                return "export default {};", "config-sha"
            return "", None

        def submit_file(_token, _repo, base, path, content, title, body, _correction_id):
            bodies.append(body)
            return {"number": len(bodies), "url": f"https://example.test/{base}", "sha": "a" * 40}

        with patch.dict(os.environ, {"GH_PAT": "token"}), \
                patch.object(submit_proofread, "load_request", return_value=request), \
                patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "update_config", return_value=("updated config", "newid")), \
                patch.object(submit_proofread, "submit_file", side_effect=submit_file), \
                redirect_stdout(io.StringIO()):
            submit_proofread.main()

        self.assertEqual(len(bodies), 2)
        for body in bodies:
            self.assertIn("## 修改内容", body)
            self.assertIn("旧文", body)
            self.assertIn("新文", body)
            self.assertIn("- 修改：正文段落 1 处", body)
            self.assertNotIn("由 BHA 校订后端提交", body)

    def test_all_changed_metadata_fields_get_text_sections(self):
        fields = {
            "title": ("旧标题", "新标题"),
            "authors": (["甲"], ["乙"]),
            "dates": ([{"year": 1966}], [{"year": 1967}]),
            "tags": ([{"name": "旧"}], [{"name": "新"}]),
            "name": ("旧来源", "新来源"),
            "author": ("旧编者", "新编者"),
            "type": ("pdf", "图片"),
            "files": (["old.pdf"], ["new.pdf"]),
        }
        request = {"changed": [
            {"kind": "metadata", "field": field, "old": old, "new": new}
            for field, (old, new) in fields.items()
        ] + [{"kind": "metadata", "field": "title", "old": "相同", "new": "相同"}]}
        body = "\n".join(submit_proofread.change_details(request))
        for label in ("标题", "作者", "日期", "标签", "来源名称", "来源作者", "来源类型", "来源文件"):
            self.assertIn(f"- {label}：", body)
        self.assertIn("删除 ~~1966年~~", body)
        self.assertIn("新增 **1967年**", body)
        self.assertNotIn("相同", body)
        comparison = []
        submit_proofread.append_metadata_comparison(comparison, request)
        comparison_body = "\n".join(comparison)
        self.assertIn("## 元数据对照", comparison_body)
        self.assertIn("| 作者 | 甲 | 乙 |", comparison_body)
        self.assertIn("| 来源文件 | old.pdf | new.pdf |", comparison_body)
        fulltext_lines = []
        submit_proofread.append_fulltext(fulltext_lines, {"fulltext": {"original": "正文", "edited": "正文"}})
        self.assertEqual(fulltext_lines, [])

    def test_metadata_lists_from_legacy_payload_show_only_semantic_changes(self):
        request = {"changed": [
            {"kind": "metadata", "field": "authors", "old": "['毛泽东']", "new": "[]"},
            {"kind": "metadata", "field": "dates", "old": "[{'year': 1967, 'month': 5, 'day': 27}]", "new": "[{'year': 1967, 'month': 5, 'day': 27}, {'year': 16}]"},
            {"kind": "metadata", "field": "tags", "old": "[{'name': '毛泽东', 'type': '人物'}, {'name': '批示', 'type': '文稿类型'}]", "new": "[{'name': '毛泽东', 'type': '人物'}]"},
        ]}
        body = "\n".join(submit_proofread.change_details(request))
        self.assertIn("删除 ~~毛泽东~~", body)
        self.assertIn("新增 **16年**", body)
        self.assertIn("删除 ~~批示（文稿类型）~~", body)
        self.assertNotIn("1967年5月27日", body)

    def test_missing_change_details_are_rebuilt_from_bha_preview(self):
        request = {
            "doc_id": "doc-1",
            "patch": {"parts": {"0": {"diff": "=4\t-1\t+新"}}, "comments": {}},
        }
        preview = {
            "article": {
                "parts": [{"text": "旧文"}],
                "comments": [],
                "comment_pivots": [{"part_idx": 0, "offset": 1, "index": 1}],
            },
            "publication_name": "来源",
            "source_files": [],
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(preview).encode()
        with patch.object(submit_proofread.urllib.request, "urlopen", return_value=response) as urlopen:
            submit_proofread.fetch_bha_changes(request)
        self.assertEqual(request["changed"], [
            {"kind": "part", "index": 1, "original": "旧〔1〕文", "edited": "旧〔1〕新"},
        ])
        self.assertEqual(request["fulltext"], {
            "original": "旧〔1〕文",
            "edited": "旧〔1〕新",
        })
        self.assertIn("/api/preview/doc-1", urlopen.call_args.args[0].full_url)

    def test_multi_operation_delta_replays_moved_text_and_utf16(self):
        self.assertEqual(submit_proofread.apply_text_delta("甲乙丙丁", "-2\t=2\t+甲乙"), "丙丁甲乙")
        self.assertEqual(submit_proofread.apply_text_delta("甲😀乙", "=1\t-2\t+校\t=1"), "甲校乙")

    def test_existing_change_details_and_fulltext_do_not_fetch_bha_preview(self):
        request = {"doc_id": "doc-1", "changed": [{"kind": "part"}], "fulltext": {"original": "旧", "edited": "新"}}
        with patch.object(submit_proofread.urllib.request, "urlopen") as urlopen:
            submit_proofread.fetch_bha_changes(request)
        urlopen.assert_not_called()

    def test_segmented_dispatch_payload_is_reassembled_and_verified(self):
        restored = {
            "kind": "proofread", "archive_id": 3, "request_id": "request-1",
            "body": {"patch": {"version": 2, "parts": {"0": {"diff": "+校订"}}, "comments": {}}},
        }
        serialized = json.dumps(restored, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        chunks = [serialized[:35], serialized[35:]]
        shas = ["a" * 40, "b" * 40]
        event = {"client_payload": {
            "payload_blobs": shas,
            "payload_sha256": submit_proofread.hashlib.sha256(serialized.encode()).hexdigest(),
            "payload_characters": len(serialized),
            "request_id": "request-1",
        }}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(event, handle)
            event_path = handle.name
        responses = [
            {"encoding": "base64", "content": base64.b64encode(chunk.encode()).decode() + "\n"}
            for chunk in chunks
        ]
        try:
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path, "TRACKER_TOKEN": "token"}), \
                    patch.object(submit_proofread, "response_or_fail", side_effect=responses) as request_api:
                request = submit_proofread.load_request()
        finally:
            os.unlink(event_path)
        self.assertEqual(request, restored)
        self.assertEqual(request_api.call_count, 2)
        self.assertTrue(request_api.call_args_list[0].args[2].endswith(f"/git/blobs/{shas[0]}"))

    def test_segmented_dispatch_rejects_hash_mismatch(self):
        request = {
            "payload_blobs": ["a" * 40], "payload_sha256": "0" * 64,
            "payload_characters": 2, "request_id": "request-1",
        }
        blob = {"encoding": "base64", "content": base64.b64encode(b"{}").decode()}
        with patch.dict(os.environ, {"TRACKER_TOKEN": "token"}), \
                patch.object(submit_proofread, "response_or_fail", return_value=blob):
            with self.assertRaisesRegex(RuntimeError, "hash does not match"):
                submit_proofread.segmented_request(request)

    def test_review_command_approve_merges_open_pulls(self):
        issue = {"number": 9, "body": "<!-- proofreading-prs:[{\"repo\":\"banned-historical-archives3\",\"number\":5,\"url\":\"u\"}] -->"}
        comment = {"user": {"login": "reviewer"}, "body": "/approve"}
        event = {"issue": issue, "comment": comment}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(event, handle)
            event_path = handle.name
        try:
            with patch.dict(os.environ, {"GH_PAT": "archive", "TRACKER_TOKEN": "tracker", "GITHUB_EVENT_PATH": event_path}), \
                    patch.object(review_proofread, "authorized", return_value=True), \
                    patch.object(review_proofread, "pull_data", return_value={"state": "open", "merged": False}), \
                    patch.object(review_proofread, "merge_pull", return_value=True) as merge, \
                    patch.object(review_proofread, "delete_pull_branch") as delete, \
                    patch.object(review_proofread, "response_or_fail") as tracker:
                with redirect_stdout(io.StringIO()):
                    review_proofread.main()
        finally:
            os.unlink(event_path)
        merge.assert_called_once_with("archive", "banned-historical-archives3", {"repo": "banned-historical-archives3", "number": 5, "url": "u"})
        delete.assert_called_once()
        self.assertEqual(tracker.call_count, 2)
        body = tracker.call_args_list[0].args[4]["body"]
        self.assertIn("banned-historical-archives3#5: merged", body)
        self.assertEqual(tracker.call_args_list[1].args[4], {"state": "closed", "state_reason": "completed"})

    def test_review_command_reject_closes_pulls_and_records_reason(self):
        issue = {"number": 9, "body": "<!-- proofreading-prs:[{\"repo\":\"banned-historical-archives3\",\"number\":5,\"url\":\"u\"}] -->"}
        comment = {"user": {"login": "reviewer"}, "body": "/reject 与原文不符"}
        event = {"issue": issue, "comment": comment}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(event, handle)
            event_path = handle.name
        try:
            with patch.dict(os.environ, {"GH_PAT": "archive", "TRACKER_TOKEN": "tracker", "GITHUB_EVENT_PATH": event_path}), \
                    patch.object(review_proofread, "authorized", return_value=True), \
                    patch.object(review_proofread, "pull_data", return_value={"state": "open", "merged": False}), \
                    patch.object(review_proofread, "close_pull", return_value=True) as close, \
                    patch.object(review_proofread, "delete_pull_branch") as delete, \
                    patch.object(review_proofread, "response_or_fail") as tracker:
                with redirect_stdout(io.StringIO()):
                    review_proofread.main()
        finally:
            os.unlink(event_path)
        close.assert_called_once_with("archive", "banned-historical-archives3", 5)
        delete.assert_called_once()
        self.assertEqual(tracker.call_count, 2)
        body = tracker.call_args_list[0].args[4]["body"]
        self.assertIn("已拒绝本次校订", body)
        self.assertIn("banned-historical-archives3#5: closed", body)
        self.assertIn("原因：与原文不符", body)
        self.assertEqual(tracker.call_args_list[1].args[4], {"state": "closed", "state_reason": "completed"})

    def test_clean_pull_request_is_automatically_merged(self):
        pull = {"number": 7, "url": "https://example.test/pr/7", "sha": "a" * 40}
        details = {
            "number": 7, "title": "校订", "merged": False, "mergeable": True,
            "mergeable_state": "clean", "head": {"sha": "a" * 40},
        }
        with patch.object(submit_proofread, "response_or_fail", return_value=details), \
                patch.object(submit_proofread, "api_request", return_value=(200, {"merged": True})):
            self.assertTrue(submit_proofread.merge_pull("token", "repo", pull))

    def test_auto_merge_policy_allows_body_text_and_limits_net_paragraph_delta(self):
        patch = {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}, "description": ""}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", patch, None))
        self.assertFalse(submit_proofread.auto_merge_allowed("proofread", patch, {"article": {"title": "新"}}))
        delete_four = {str(index): {"delete": True} for index in range(4)}
        self.assertFalse(submit_proofread.auto_merge_allowed("proofread", {"version": 2, "parts": delete_four, "comments": {}, "description": ""}, None))
        delete_three = {str(index): {"delete": True} for index in range(3)}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", {"version": 2, "parts": delete_three, "comments": {}, "description": ""}, None))
        four_inserts = {"0": {"diff": "-1\t+新", "insertAfter": [{"text": "甲"}, {"text": "乙"}, {"text": "丙"}, {"text": "丁"}]}}
        self.assertFalse(submit_proofread.auto_merge_allowed("proofread", {"version": 2, "parts": four_inserts, "comments": {}, "description": ""}, None))
        balanced = {str(index): {"diff": "-1\t+新", "delete": True, "insertAfter": [{"text": "换"}]} for index in range(5)}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", {"version": 2, "parts": balanced, "comments": {}, "description": ""}, None))
        many_text = {str(index): {"diff": "-1\t+新"} for index in range(10)}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", {"version": 2, "parts": many_text, "comments": {}, "description": ""}, None))
        type_change = {"0": {"diff": "-1\t+新", "type": "title"}, "1": {"diff": "-2\t+改"}}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", {"version": 2, "parts": type_change, "comments": {}, "description": ""}, None))
        type_only = {"0": {"type": "title"}, "1": {"type": "subdate"}}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", {"version": 2, "parts": type_only, "comments": {}, "description": ""}, None))
        at_limit = {"version": 2, "parts": {"0": {"diff": "-500"}}, "comments": {}, "description": ""}
        over_limit = {"version": 2, "parts": {"0": {"diff": "-501"}}, "comments": {}, "description": ""}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", at_limit, None))
        self.assertFalse(submit_proofread.auto_merge_allowed("proofread", over_limit, None))
        inserted_at_limit = {"version": 2, "parts": {"0": {"insertAfter": [{"text": "新" * 500, "type": "paragraph"}]}}, "comments": {}, "description": ""}
        inserted_over_limit = {"version": 2, "parts": {"0": {"insertAfter": [{"text": "新" * 501, "type": "paragraph"}]}}, "comments": {}, "description": ""}
        self.assertTrue(submit_proofread.auto_merge_allowed("proofread", inserted_at_limit, None))
        self.assertFalse(submit_proofread.auto_merge_allowed("proofread", inserted_over_limit, None))

    def test_existing_pull_must_contain_requested_content(self):
        existing = {
            "number": 7, "url": "https://example.test/pr/7", "base": "ocr_patch",
            "head": "proofread/correction-ocr_patch",
        }
        with patch.object(submit_proofread, "open_pull_request", return_value=existing), \
                patch.object(submit_proofread, "get_file", side_effect=[("base", "sha"), ("unexpected", "branch-sha")]):
            with self.assertRaisesRegex(RuntimeError, "requested file content"):
                submit_proofread.submit_file(
                    "token", "repo", "ocr_patch", "archives0/[a][p].ts", "expected",
                    "title", "description", "correction",
                )

    def test_existing_batch_pull_must_match_all_requested_files(self):
        existing = {
            "number": 8, "url": "https://example.test/pr/8", "base": "ocr_patch",
            "head": "proofread/correction-ocr_patch",
        }
        contents = {"[a][p].ts": "one", "[b][p].ts": "two"}

        def get_file(_token, _repo, _branch, path):
            return contents[path], "sha"

        with patch.object(submit_proofread, "open_pull_request", return_value=existing), \
                patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "api_request", return_value=(200, [
                    {"filename": "[a][p].ts"}, {"filename": "[b][p].ts"},
                ])):
            result = submit_proofread.submit_files(
                "token", "repo", "ocr_patch", contents, "title", "description", "correction",
            )
        self.assertEqual(result, existing)

    def test_auto_merge_notification_is_idempotent(self):
        issue = {"number": 12, "html_url": "https://example.test/issues/12"}
        request = {"doc_id": "doc-1", "patch": {"parts": {"0": {"diff": "-旧\t+新"}}}}
        pulls = [{"number": 7, "url": "https://example.test/pr/7"}]
        with patch.object(submit_proofread, "ensure_auto_merge_log", return_value=issue), \
                patch.object(submit_proofread, "auto_merge_comment_exists", return_value=True), \
                patch.object(submit_proofread, "response_or_fail") as request_api:
            url = submit_proofread.notify_auto_merged(
                "token", "correction", request, "banned-historical-archives3", "article-1", pulls,
            )
        self.assertEqual(url, issue["html_url"])
        request_api.assert_not_called()

    def test_auto_merge_notification_records_pull_and_marker(self):
        issue = {"number": 12, "html_url": "https://example.test/issues/12"}
        request = {
            "doc_id": "doc-1",
            "patch": {"parts": {"0": {"diff": "-旧\t+新"}}},
            "changed": [
                {"kind": "part", "index": 1, "original": "旧文", "edited": "新文"},
                {"kind": "metadata", "field": "title", "old": "旧标题", "new": "新标题"},
            ],
            "fulltext": {"original": "旧文", "edited": "新文"},
        }
        pulls = [{"number": 7, "url": "https://example.test/pr/7"}]
        with patch.object(submit_proofread, "ensure_auto_merge_log", return_value=issue), \
                patch.object(submit_proofread, "auto_merge_comment_exists", return_value=False), \
                patch.object(submit_proofread, "response_or_fail", return_value={}) as request_api:
            url = submit_proofread.notify_auto_merged(
                "token", "correction", request, "banned-historical-archives3", "article-1", pulls,
            )
        self.assertEqual(url, issue["html_url"])
        payload = request_api.call_args.args[4]
        self.assertIn("<!-- auto-merged:correction -->", payload["body"])
        self.assertIn("Article ID：`article-1`", payload["body"])
        self.assertIn("banned-historical-archives3#7", payload["body"])
        self.assertIn("## 修改内容", payload["body"])
        self.assertIn("- 段落 1：旧文 → 新文", payload["body"])
        self.assertIn("- 标题：旧标题 → 新标题", payload["body"])
        self.assertIn("## 原全文", payload["body"])
        self.assertIn("## 修改后全文", payload["body"])

    def test_auto_merge_notification_moves_long_fulltext_to_complete_comments(self):
        issue = {"number": 12, "html_url": "https://example.test/issues/12"}
        original = "旧" * 40000
        edited = "新" * 40000
        request = {
            "doc_id": "doc-1", "changed": [{"kind": "part", "index": 1, "original": "旧", "edited": "新"}],
            "fulltext": {"original": original, "edited": edited},
        }
        pulls = [{"number": 7, "url": "https://example.test/pr/7"}]
        with patch.object(submit_proofread, "ensure_auto_merge_log", return_value=issue), \
                patch.object(submit_proofread, "auto_merge_comment_exists", return_value=False), \
                patch.object(submit_proofread, "existing_comment_bodies", return_value=[]), \
                patch.object(submit_proofread, "response_or_fail", return_value={}) as request_api:
            submit_proofread.notify_auto_merged(
                "token", "correction", request, "banned-historical-archives3", "article-1", pulls,
            )
        bodies = [call.args[4]["body"] for call in request_api.call_args_list]
        self.assertEqual(len(bodies), 3)
        self.assertLessEqual(max(map(len, bodies)), submit_proofread.GITHUB_BODY_LIMIT)
        self.assertNotIn("## 原全文", bodies[0])
        self.assertIn(original, bodies[1])
        self.assertIn(edited, bodies[2])

    def test_patch_validation_rejects_structural_noops_and_invalid_inserts(self):
        base = {"version": 2, "parts": {}, "comments": {}, "description": ""}
        invalid = [
            {**base, "parts": {"0": {"delete": False}}},
            {**base, "parts": {"0": {"insertAfter": []}}},
            {**base, "parts": {"0": {"insertAfter": ["正文"]}}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                submit_proofread.validate_patch(value)

    def test_config_helper_preserves_unrelated_comments(self):
        content = """export default {
  // keep entity note
  entity: { name: "旧来源", files: ["old"] },
  parser_option: { articles: [
    // keep article note
    { title: "旧标题", authors: ["甲"], dates: [{ year: 1967 }], page_start: 1, page_end: 2 }
  ] }
};"""
        payload = {
            "content": content,
            "article_id": "missing",
            "locator": {"title": "旧标题", "page_start": 1, "page_end": 2},
            "metadata": {
                "article": {"title": "新标题", "tags": [{"name": "标签", "type": "主题/事件"}]},
                "source": {"name": "新来源", "author": "来源作者"},
            },
        }
        helper = Path(__file__).parents[1] / "scripts" / "update_archive_config.mjs"
        process = subprocess.run(
            ["node", str(helper)], input=json.dumps(payload), text=True,
            capture_output=True, check=True,
        )
        result = json.loads(process.stdout)
        self.assertIn("keep entity note", result["content"])
        self.assertIn("keep article note", result["content"])
        self.assertIn("新标题", result["content"])
        self.assertIn("新来源", result["content"])
        self.assertIn("来源作者", result["content"])
        self.assertIn("主题/事件", result["content"])
        self.assertEqual(len(result["article_id"]), 10)
        verify = subprocess.run(
            ["node", str(helper)], input=json.dumps({
                "content": result["content"], "article_id": result["article_id"], "metadata": {},
            }), text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(verify.stdout)["article_id"], result["article_id"])

    def test_config_helper_updates_source_without_articles(self):
        content = 'export default { entity: { name: "旧来源", author: "" }, parser_option: {} };'
        payload = {
            "content": content,
            "article_id": "article-id",
            "metadata": {"source": {"author": "来源作者"}},
        }
        helper = Path(__file__).parents[1] / "scripts" / "update_archive_config.mjs"
        process = subprocess.run(
            ["node", str(helper)], input=json.dumps(payload), text=True,
            capture_output=True, check=True,
        )
        result = json.loads(process.stdout)
        self.assertIn('author: "来源作者"', result["content"])
        self.assertEqual(result["article_id"], "article-id")

    def test_legacy_article_metadata_updates_ocr_config(self):
        request = {
            "article_id": "oldid", "locator": {"title": "旧标题", "page_start": 1, "page_end": 2},
            "metadata": {"article": {"authors": ["新作者"]}},
        }
        config = 'export default { entity: { name: "来源" }, parser_option: {} };'
        ocr_config = '''export default {
  entity: { name: "来源" },
  parser_option: { articles: [
    { title: "旧标题", authors: ["旧作者"], dates: [{ year: 1967 }], page_start: 1, page_end: 2 }
  ] }
};'''

        def get_file(_token, _repo, branch, path):
            self.assertEqual(path, "publication.ts")
            return (config if branch == "config" else ocr_config), "sha"

        with patch.object(submit_proofread, "get_file", side_effect=get_file):
            files, article_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives1", "publication", request,
            )
        self.assertEqual([(base, path) for base, path, _content in files], [("ocr_config", "publication.ts")])
        self.assertIn("新作者", files[0][2])
        self.assertEqual(len(article_id), 10)

    def test_legacy_mixed_metadata_splits_ocr_config_and_config(self):
        request = {
            "article_id": "oldid", "locator": {"title": "旧标题", "page_start": 1, "page_end": 2},
            "metadata": {
                "article": {"authors": ["新作者"]},
                "source": {"author": "新来源作者"},
            },
        }
        config = 'export default { entity: { name: "来源", author: "" }, parser_option: {} };'
        ocr_config = '''export default {
  entity: { name: "来源" },
  parser_option: { articles: [
    { title: "旧标题", authors: ["旧作者"], dates: [{ year: 1967 }], page_start: 1, page_end: 2 }
  ] }
};'''

        def get_file(_token, _repo, branch, _path):
            return (config if branch == "config" else ocr_config), "sha"

        with patch.object(submit_proofread, "get_file", side_effect=get_file):
            files, _article_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives1", "publication", request,
            )
        self.assertEqual([base for base, _path, _content in files], ["ocr_config", "config"])
        self.assertIn("新作者", files[0][2])
        self.assertIn("新来源作者", files[1][2])

    def test_rmrb_database_author_updates_origin_json(self):
        source = {
            "title": "旧标题", "authors": ["旧作者"],
            "dates": [{"year": 1967, "month": 5, "day": 16}],
            "parts": [], "page_start": 1, "page_end": 1,
        }
        article_id = submit_proofread.normalized_article_id(source)
        request = {
            "article_id": article_id,
            "locator": {"title": "旧标题", "authors": ["旧作者"], "dates": source["dates"], "page_start": 1, "page_end": 1},
            "metadata": {"article": {"authors": ["新作者"]}},
        }
        config = 'export default { "parser_id": "rmrb", "entity": { "type": "db" }, "parser_option": {} };'
        raw = json.dumps(source, ensure_ascii=False, separators=(",", ":"))

        def get_file(_token, _repo, branch, path):
            if branch == "config":
                return config, "config-sha"
            self.assertEqual((branch, path), ("origin", "json/1967/5/7.json"))
            return raw, "origin-sha"

        with patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "db_source_paths", return_value=["json/1967/5/7.json"]):
            files, new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives10", "rmrb", request,
            )
        self.assertEqual(files[0][:2], ("origin", "json/1967/5/7.json"))
        self.assertEqual(json.loads(files[0][2])["authors"], ["新作者"])
        self.assertNotEqual(new_id, article_id)

    def test_whb_database_author_preserves_source_fields(self):
        raw = {
            "id": 1234, "ytitle": "主", "mtitle": "题", "ftitle": "",
            "authors": ["旧作者"], "date": [{"year": 1967, "month": 1, "day": 2}],
            "source": "新华社", "text": [{"type": "paragraph", "text": "正文"}],
        }
        article = submit_proofread.decode_db_source("whb", json.dumps(raw, ensure_ascii=False))
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "locator": {"title": "主题", "authors": ["旧作者"], "dates": article["dates"], "page_start": 1, "page_end": 1},
            "metadata": {"article": {"authors": ["新作者"]}},
        }
        config = 'export default { parser_id: "whb", entity: { type: "db" }, parser_option: {} };'

        def get_file(_token, _repo, branch, _path):
            return (config if branch == "config" else json.dumps(raw, ensure_ascii=False)), "sha"

        with patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "db_source_paths", return_value=["json/1/1234.json"]):
            files, _new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives20", "whb", request,
            )
        updated = json.loads(files[0][2])
        self.assertEqual(updated["authors"], ["新作者"])
        self.assertEqual(updated["source"], "新华社")
        self.assertEqual(updated["text"], raw["text"])

    def test_jfjb_database_author_round_trips_gb2312(self):
        text = "\n".join([
            "〖RQ/日期〗19690605〖-RQ/日期〗", "〖BH/版号〗02〖-BH/版号〗",
            "〖BT/标题〗旧标题〖-BT/标题〗", "〖ZZ/作者〗旧作者〖-ZZ/作者〗",
            "〖ZW/正文〗正文〖-ZW/正文〗",
        ])
        encoded = text.encode("gb2312")
        article = submit_proofread.decode_db_source("jfjb", encoded)
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "locator": {"title": "旧标题", "authors": ["旧作者"], "dates": article["dates"], "page_start": 2, "page_end": 2},
            "metadata": {"article": {"authors": ["新作者", "作者乙"]}},
        }
        config = 'export default { parser_id: "jfjb", entity: { type: "db" }, parser_option: {} };'
        with patch.object(submit_proofread, "get_file", return_value=(config, "sha")), \
                patch.object(submit_proofread, "get_file_bytes", return_value=(encoded, "sha")), \
                patch.object(submit_proofread, "db_source_paths", return_value=["txt/1969/196906/19690605/196906050201.TXT"]):
            files, _new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives24", "jfjb", request,
            )
        self.assertIsInstance(files[0][2], bytes)
        decoded = files[0][2].decode("gb2312")
        self.assertIn("〖ZZ/作者〗新作者 作者乙〖-ZZ/作者〗", decoded)
        self.assertIn("〖ZW/正文〗正文〖-ZW/正文〗", decoded)

    def test_database_metadata_rejects_fields_without_reversible_source_mapping(self):
        with self.assertRaisesRegex(RuntimeError, "authors only"):
            submit_proofread.update_db_source("rmrb", "{}", {"title": "新标题"})

    def test_maoistlegacy_metadata_updates_main_meta_json(self):
        raw = {
            "title": "旧标题", "creator": ["旧作者"], "dates": [{"year": 1958}],
            "tags": ["旧标签"], "source": ["来源"], "parts": [{"type": "paragraph", "text": "正文"}],
        }
        article = {"title": raw["title"], "authors": raw["creator"], "dates": raw["dates"], "is_range_date": False}
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "metadata": {"article": {
                "title": "新标题", "authors": ["新作者"], "dates": [{"year": 1959}],
                "tags": [{"name": "新标签", "type": "主题/事件"}],
            }},
        }
        config = 'export default { parser_id: "maoistlegacy-txt", path: "data/1000", entity: {}, parser_option: {} };'

        def get_file(_token, _repo, branch, path):
            if branch == "config":
                return config, "config-sha"
            self.assertEqual((branch, path), ("main", "data/1000/meta.json"))
            return json.dumps(raw, ensure_ascii=False), "main-sha"

        with patch.object(submit_proofread, "get_file", side_effect=get_file):
            files, new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives11", "maoistlegacy.de-1000", request,
            )
        self.assertEqual(files[0][:2], ("main", "data/1000/meta.json"))
        updated = json.loads(files[0][2])
        self.assertEqual(updated["creator"], ["新作者"])
        self.assertEqual(updated["tags"], ["新标签"])
        self.assertEqual(updated["parts"], raw["parts"])
        self.assertNotEqual(new_id, request["article_id"])

    def test_maoistlegacy_rejects_non_subject_tags(self):
        with self.assertRaisesRegex(RuntimeError, "subject tags only"):
            submit_proofread.update_structured_source(
                "maoistlegacy-txt", '{"title":"标题","creator":[],"dates":[]}', 0,
                {"tags": [{"name": "人物", "type": "人物"}]},
            )

    def test_result_json_updates_one_array_article_and_preserves_others(self):
        articles = [
            {"title": "第一篇", "authors": ["甲"], "dates": [{"year": 1967}], "parts": []},
            {"title": "第二篇", "authors": ["乙"], "dates": [{"year": 1968}], "parts": [{"text": "正文"}]},
        ]
        request = {
            "article_id": submit_proofread.normalized_article_id(articles[1]),
            "metadata": {"article": {"authors": ["新作者"], "title": "新标题"}},
        }
        config = 'export default { parser_id: "result-json", path: "collection", entity: {}, parser_option: {} };'

        def get_file(_token, _repo, branch, path):
            if branch == "config":
                return config, "config-sha"
            self.assertEqual((branch, path), ("main", "collection/one.json"))
            return json.dumps(articles, ensure_ascii=False), "main-sha"

        with patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "list_directory", return_value=["collection/one.json"]):
            files, _new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives18", "publication", request,
            )
        updated = json.loads(files[0][2])
        self.assertEqual(files[0][:2], ("main", "collection/one.json"))
        self.assertEqual(updated[0], articles[0])
        self.assertEqual(updated[1]["authors"], ["新作者"])
        self.assertEqual(updated[1]["parts"], articles[1]["parts"])

    def test_result_json_rejects_ambiguous_article_identity(self):
        article = {"title": "同一篇", "authors": [], "dates": []}
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "metadata": {"article": {"authors": ["新作者"]}},
        }
        config = 'export default { parser_id: "result-json", path: "collection", entity: {}, parser_option: {} };'
        with patch.object(submit_proofread, "get_file", side_effect=[
                    (config, "sha"), (json.dumps([article]), "sha"), (json.dumps([article]), "sha"),
                ]), patch.object(
                    submit_proofread, "list_directory", return_value=["collection/one.json", "collection/two.json"],
                ):
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                submit_proofread.update_metadata_files(
                    "token", "banned-historical-archives14", "maoistlibrary", request,
                )

    def test_result_json_v2_updates_direct_main_file(self):
        article = {"title": "旧标题", "authors": ["旧作者"], "dates": [{"year": 1971}], "parts": []}
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "metadata": {"article": {"dates": [{"year": 1972}]}},
        }
        config = 'export default { parser_id: "result-json-v2", path: "publication.json", entity: {}, parser_option: { articles: [] } };'

        def get_file(_token, _repo, branch, path):
            return (config, "sha") if branch == "config" else (json.dumps(article, ensure_ascii=False), "sha")

        with patch.object(submit_proofread, "get_file", side_effect=get_file):
            files, new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives21", "publication", request,
            )
        self.assertEqual(files[0][:2], ("main", "publication.json"))
        self.assertEqual(json.loads(files[0][2])["dates"], [{"year": 1972}])
        self.assertNotEqual(new_id, request["article_id"])

    def test_ccrd_updates_unique_main_json_article(self):
        raw = {
            "title": "旧标题", "authors": ["旧作者"], "date": "1967-5-0",
            "contents": [{"type": "paragraph", "text": "正文"}],
        }
        article = submit_proofread.direct_source_article("CCRD", json.dumps(raw, ensure_ascii=False))
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "locator": {"title": "旧标题", "authors": ["旧作者"], "dates": article["dates"]},
            "metadata": {"article": {"title": "新标题", "authors": ["新作者"]}},
        }
        config = 'export default { parser_id: "CCRD", path: "CCRD/2", entity: {}, parser_option: {} };'

        def get_file(_token, _repo, branch, path):
            if branch == "config":
                return config, "config-sha"
            self.assertEqual((branch, path), ("main", "CCRD/2/0/0/0.json"))
            return json.dumps(raw, ensure_ascii=False), "main-sha"

        with patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "searched_source_paths", return_value=["CCRD/2/0/0/0.json"]):
            files, new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives9", "CCRD-dayuejin", request,
            )
        updated = json.loads(files[0][2])
        self.assertEqual(files[0][:2], ("main", "CCRD/2/0/0/0.json"))
        self.assertEqual(updated["title"], "新标题")
        self.assertEqual(updated["authors"], ["新作者"])
        self.assertEqual(updated["date"], "1967-5-0")
        self.assertEqual(updated["contents"], raw["contents"])
        self.assertNotEqual(new_id, request["article_id"])

    def test_ccrd_rejects_non_reversible_date_update(self):
        with self.assertRaisesRegex(RuntimeError, "title and authors only"):
            submit_proofread.update_direct_source("CCRD", '{"date":"1967-5-0"}', {"dates": [{"year": 1967}]})

    def test_aisixiang_updates_identity_nodes_without_reserializing_html(self):
        source = '''<!doctype html><html><body>
<div class="show_text"><h3>旧&amp;标题</h3><div class="info">更新时间：2017-09-26 01:25</div></div>
<div class="about"><strong>旧作者</strong></div><div class="article-content"><p>正文<strong>粗体</strong></p></div>
</body></html>'''
        article = submit_proofread.direct_source_article("aisixiang", source)
        self.assertEqual(article["title"], "旧&标题")
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "locator": {"title": "旧&标题", "authors": ["旧作者"], "dates": article["dates"]},
            "metadata": {"article": {
                "title": "新&标题", "authors": ["新作者"],
                "dates": [{"year": 2018, "month": 10, "day": 7}],
            }},
        }
        config = 'export default { parser_id: "aisixiang", path: "html", entity: {}, parser_option: {} };'

        def get_file(_token, _repo, branch, path):
            return (config, "config-sha") if branch == "config" else (source, "main-sha")

        with patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "searched_source_paths", return_value=["html/100267.html"]):
            files, new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives31", "aisixiang", request,
            )
        updated = files[0][2]
        self.assertIn("<h3>新&amp;标题</h3>", updated)
        self.assertIn("<strong>新作者</strong>", updated)
        self.assertIn("2018-10-07 01:25", updated)
        self.assertIn("正文<strong>粗体</strong>", updated)
        self.assertNotEqual(new_id, request["article_id"])

    def test_aisixiang_requires_one_author_and_rejects_tags(self):
        source = '<h3>标题</h3><div class="info">2020-01-02</div><strong>作者</strong>'
        with self.assertRaisesRegex(RuntimeError, "exactly one author"):
            submit_proofread.update_direct_source("aisixiang", source, {"authors": ["甲", "乙"]})
        with self.assertRaisesRegex(RuntimeError, "title, authors, and dates only"):
            submit_proofread.update_direct_source("aisixiang", source, {"tags": []})

    def test_direct_source_search_is_bounded_and_scoped(self):
        config = 'export default { parser_id: "aisixiang", path: "html" };'
        with patch.object(submit_proofread, "api_request", return_value=(
                    200, {"total_count": 1, "items": [{"path": "html/1.html"}]},
                )) as api:
            paths = submit_proofread.searched_source_paths(
                "token", "banned-historical-archives31", "aisixiang", config, {"title": "旧标题"},
            )
        self.assertEqual(paths, ["html/1.html"])
        query = api.call_args.args[2]
        self.assertIn("path%3Ahtml", query)
        self.assertIn("extension%3Ahtml", query)

    def test_cnd_updates_only_the_uniquely_matched_article_author(self):
        source = """index
<a name=\"one\">~{第一篇~}~{ ·作者甲·~}
   第一篇正文

<a name=\"two\">~{第二篇~}~{ ·作者乙·~}
   第二篇正文
"""
        parsed = submit_proofread.cnd_source_articles(source)
        self.assertEqual([article[2]["title"] for article in parsed], ["第一篇", "第二篇"])
        article = parsed[1][2]
        request = {
            "article_id": submit_proofread.normalized_article_id(article),
            "locator": {"title": "第二篇", "authors": ["作者乙"], "dates": []},
            "metadata": {"article": {"authors": ["新作者", "作者丙"]}},
        }
        config = 'export default { parser_id: "CND", path: "", entity: {}, parser_option: {} };'

        def get_file(_token, _repo, branch, path):
            return (config, "config-sha") if branch == "config" else (source, "main-sha")

        with patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "searched_source_paths", return_value=["html/CR/ZK00/one.hz8.html"]):
            files, new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives12", "cnd", request,
            )
        updated = files[0][2]
        self.assertIn("·作者甲·~}", updated)
        self.assertIn("·新作者·作者丙·~}", updated)
        self.assertIn("第二篇正文", updated)
        self.assertNotEqual(new_id, request["article_id"])

    def test_cnd_rejects_unsupported_fields_and_marker_characters(self):
        with self.assertRaisesRegex(RuntimeError, "authors only"):
            submit_proofread.update_cnd_source("index<a name=x>", 1, 0, {"title": "新标题"})
        with self.assertRaisesRegex(RuntimeError, "authors are invalid"):
            submit_proofread.update_cnd_source(
                "index<a name=x>~{标题~}~{ ·作者·~}\n", 1, 0, {"authors": ["甲·乙"]},
            )

    def test_cnd_source_search_uses_parser_hardcoded_root(self):
        config = 'export default { parser_id: "CND", path: "" };'
        with patch.object(submit_proofread, "api_request", return_value=(
                    200, {"total_count": 1, "items": [{"path": "html/CR/ZK00/one.hz8.html"}]},
                )) as api:
            paths = submit_proofread.searched_source_paths(
                "token", "banned-historical-archives12", "CND", config, {"title": "标题"},
            )
        self.assertEqual(paths, ["html/CR/ZK00/one.hz8.html"])
        self.assertIn("path:html/CR", __import__("urllib.parse").parse.unquote(api.call_args.args[2]))

    def test_pdf_parser_writes_consumed_metadata_override(self):
        request = {
            "article_id": "oldarticle",
            "locator": {
                "title": "旧标题", "authors": ["旧作者"], "dates": [{"year": 1967}],
                "is_range_date": False, "page_start": 1, "page_end": 2,
            },
            "metadata": {"article": {"authors": ["新作者"], "title": "新标题"}},
        }
        config = 'export default { parser_id: "wenji", path: "books/wenji1.pdf", entity: {}, parser_option: {} };'
        with patch.object(submit_proofread, "get_file", side_effect=[(config, "sha"), ("", None)]), \
                patch.object(submit_proofread, "list_directory", return_value=[]):
            files, new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives9", "wenji1", request,
            )
        self.assertEqual(files[0][:2], ("config", "metadata_overrides/wenji1/oldarticle.json"))
        override = json.loads(files[0][2])
        self.assertEqual(override["metadata"], request["metadata"]["article"])
        self.assertEqual(override["new_article_id"], new_id)

    def test_override_parser_mixed_source_metadata_uses_one_config_batch(self):
        request = {
            "kind": "proofread", "archive_id": 9, "article_id": "oldarticle", "publication_id": "wenji1",
            "locator": {"title": "旧标题", "authors": [], "dates": []},
            "metadata": {"article": {"authors": ["新作者"]}, "source": {"author": "新来源作者"}},
        }
        config = 'export default { parser_id: "wenji", path: "books/wenji1.pdf", entity: { author: "旧作者" }, parser_option: {} };'
        with patch.dict(os.environ, {"GH_PAT": "token"}), \
                patch.object(submit_proofread, "load_request", return_value=request), \
                patch.object(submit_proofread, "get_file", side_effect=lambda _t, _r, branch, path: (config, "sha") if branch == "config" and path == "wenji1.ts" else ("", None)), \
                patch.object(submit_proofread, "list_directory", return_value=[]), \
                patch.object(submit_proofread, "fetch_bha_changes"), \
                patch.object(submit_proofread, "submit_files", return_value={"number": 1, "url": "https://example/config"}) as batch, \
                patch.object(submit_proofread, "submit_file") as single, \
                redirect_stdout(io.StringIO()):
            submit_proofread.main()
        batch.assert_called_once()
        self.assertEqual(set(batch.call_args.args[3]), {
            "metadata_overrides/wenji1/oldarticle.json", "wenji1.ts",
        })
        single.assert_not_called()

    def test_override_parser_updates_existing_identity_chain(self):
        previous = {
            "version": 1, "publication_id": "wenji1", "article_id": "oldarticle",
            "new_article_id": "currentid", "article": {
                "title": "原标题", "authors": ["原作者"], "dates": [], "is_range_date": False,
            },
            "metadata": {"title": "当前标题"},
        }
        request = {
            "article_id": "currentid",
            "locator": {"title": "当前标题", "authors": ["原作者"], "dates": [], "is_range_date": False},
            "metadata": {"article": {"authors": ["新作者"]}},
        }
        config = 'export default { parser_id: "wenji", path: "books/wenji1.pdf", entity: {}, parser_option: {} };'

        def get_file(_token, _repo, _branch, path):
            if path == "wenji1.ts":
                return config, "sha"
            if path.endswith("oldarticle.json"):
                return json.dumps(previous, ensure_ascii=False), "sha"
            return "", None

        with patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(
                    submit_proofread, "list_directory",
                    return_value=["metadata_overrides/wenji1/oldarticle.json"],
                ):
            files, _new_id = submit_proofread.update_metadata_files(
                "token", "banned-historical-archives9", "wenji1", request,
            )
        self.assertEqual(files[0][1], "metadata_overrides/wenji1/oldarticle.json")
        updated = json.loads(files[0][2])
        self.assertEqual(updated["article_id"], "oldarticle")
        self.assertEqual(updated["article"]["title"], "原标题")
        self.assertEqual(updated["metadata"], {"title": "当前标题", "authors": ["新作者"]})

    def test_large_source_file_falls_back_to_git_blob(self):
        encoded = base64.b64encode("大文件".encode()).decode()
        with patch.object(submit_proofread, "api_request", side_effect=[
                    (200, {"encoding": "none", "content": "", "sha": "a" * 40}),
                    (200, {"encoding": "base64", "content": encoded, "sha": "a" * 40}),
                ]) as api:
            content, sha = submit_proofread.get_file_bytes("token", "repo", "main", "large.json")
        self.assertEqual(content.decode(), "大文件")
        self.assertEqual(sha, "a" * 40)
        self.assertIn("/git/blobs/", api.call_args_list[1].args[2])

    def test_whb_source_search_falls_back_to_split_title_fragment(self):
        responses = [
            (200, {"total_count": 0, "items": []}),
            (200, {"total_count": 1, "items": [{"path": "json/1/1234.json"}]}),
        ]
        locator = {
            "title": "这是很长的引题这是很长的主题这是很长的副题",
            "dates": [{"year": 1967, "month": 1, "day": 2}],
        }
        with patch.object(submit_proofread, "api_request", side_effect=responses) as api:
            paths = submit_proofread.db_source_paths(
                "token", "banned-historical-archives20", "whb", locator,
            )
        self.assertEqual(paths, ["json/1/1234.json"])
        self.assertEqual(api.call_count, 2)

    def test_parse_helper_replaces_one_placeholder_article(self):
        existing = '''export default {
  "entity": {"id": "publication"},
  "parser_option": {
    "articles": [{
      "title": "【文章待拆分】小报",
      "authors": [],
      "page_start": 1,
      "page_end": 2,
      "dates": [{"year": 1967}]
    }],
    "ocr": {"use_onnx": true}
  }
};'''
        request = {
            "locator": {"title": "【文章待拆分】小报", "page_start": 1, "page_end": 2},
            "articles": [
                {"title": "第一篇", "authors": [], "dates": [{"year": 1967}], "page_start": 1, "page_end": 1, "content": "第一篇正文", "base_part_count": 2},
                {"title": "第二篇", "authors": [], "dates": [{"year": 1967}], "page_start": 2, "page_end": 2, "content": "第二篇正文", "base_part_count": 3},
            ],
        }
        content = submit_proofread.replace_config_articles(existing, request)
        self.assertNotIn("【文章待拆分】", content)
        self.assertIn('"title": "第一篇"', content)
        self.assertIn('"title": "第二篇"', content)
        self.assertIn('"ocr": {"use_onnx": true}', content)
        self.assertNotIn("base_part_count", content)
        self.assertNotIn("第一篇正文", content)

    def test_parse_validation_rejects_missing_pages_and_duplicate_ids(self):
        request = {
            "locator": {"title": "【文章待拆分】小报", "page_start": 1, "page_end": 2},
            "source_files": ["one", "two"],
            "articles": [{"title": "第一篇", "authors": [], "dates": [], "page_start": 1, "page_end": 1, "content": "正文", "base_part_count": 1}],
        }
        with self.assertRaisesRegex(RuntimeError, "do not cover"):
            submit_proofread.validate_parse_request(request, 25)
        request["articles"] = [
            {"title": "同题", "authors": [], "dates": [], "page_start": 1, "page_end": 1, "content": "正文一", "base_part_count": 1},
            {"title": "同题", "authors": [], "dates": [], "page_start": 2, "page_end": 2, "content": "正文二", "base_part_count": 1},
        ]
        with self.assertRaisesRegex(RuntimeError, "duplicate article IDs"):
            submit_proofread.validate_parse_request(request, 25)

    def test_manual_parse_content_patch_replaces_every_generated_part(self):
        patch_value = submit_proofread.manual_content_patch({
            "content": "第一段\n第二段", "base_part_count": 3,
        })
        self.assertEqual(patch_value["version"], 2)
        self.assertEqual(set(patch_value["parts"]), {"0", "1", "2"})
        self.assertTrue(all(value["delete"] for value in patch_value["parts"].values()))
        self.assertEqual(patch_value["parts"]["0"]["insertBefore"], [
            {"type": "paragraph", "text": "第一段"},
            {"type": "paragraph", "text": "第二段"},
        ])

    def test_parse_submission_creates_config_and_batch_ocr_patch_pulls(self):
        publication_id = "052417de-42fb-4781-9885-af4fb006e9b6"
        request = {
            "kind": "parse", "archive_id": 25, "publication_id": publication_id,
            "locator": {"title": "【文章待拆分】小报", "page_start": 1, "page_end": 2},
            "source_files": ["one", "two"],
            "articles": [
                {"title": "第一篇", "authors": [], "dates": [{"year": 1967}], "page_start": 1, "page_end": 1, "content": "第一篇正文", "base_part_count": 2, "ocr": {"content_thresholds": [0.1, 0.1, 0.1, 0.1]}},
                {"title": "第二篇", "authors": [], "dates": [{"year": 1967}], "page_start": 2, "page_end": 2, "content": "第二篇正文", "base_part_count": 3},
            ],
        }

        def get_file(_token, _repo, branch, _path):
            if branch == "config":
                return "existing config", "config-sha"
            return "", None

        with patch.dict(os.environ, {"GH_PAT": "token"}), \
                patch.object(submit_proofread, "load_request", return_value=request), \
                patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "replace_config_articles", return_value="updated config"), \
                patch.object(submit_proofread, "build_review_crops", return_value=[{"article_index": 0, "title": "第一篇", "pages": [1], "url": "https://example/crop.webp"}]), \
                patch.object(submit_proofread, "submit_file", return_value={"number": 1, "url": "https://example/config"}) as config_submit, \
                patch.object(submit_proofread, "submit_files", return_value={"number": 2, "url": "https://example/patch"}) as patch_submit, \
                patch.object(submit_proofread, "upsert_parse_tracker_issue", return_value="https://example/issue") as tracker, \
                patch.dict(os.environ, {"TRACKER_TOKEN": "tracker"}), \
                redirect_stdout(io.StringIO()) as output:
            submit_proofread.main()
        config_submit.assert_called_once()
        contents = patch_submit.call_args.args[3]
        self.assertEqual(len(contents), 2)
        self.assertTrue(all(path.endswith(f"][{publication_id}].ts") for path in contents))
        self.assertTrue(any("第一篇正文" in content for content in contents.values()))
        self.assertIn("OCR 裁剪原图", config_submit.call_args.args[6])
        self.assertIn("第一篇正文", config_submit.call_args.args[6])
        self.assertIn("第二篇正文", patch_submit.call_args.args[5])
        self.assertIn("https://example/crop.webp", patch_submit.call_args.args[5])
        tracker.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertEqual(result["pull_requests"], [
            "https://example/config", "https://example/patch",
        ])
        self.assertEqual(result["tracker_issue"], "https://example/issue")

    def test_parse_review_body_moves_oversized_fulltext_to_pull_comments(self):
        request = {
            "locator": {"title": "【文章待拆分】小报", "page_start": 1, "page_end": 1},
            "articles": [{
                "title": "第一篇", "authors": [], "dates": [], "page_start": 1, "page_end": 1,
                "content": "正文" * 40_000, "base_part_count": 1,
            }],
        }
        body = submit_proofread.parse_review_body(
            request, "banned-historical-archives25", "publication", "correction", [],
        )
        self.assertNotIn("<!-- parse-fulltext:embedded -->", body)
        self.assertIn("已按文章分段保存在本 PR 评论中", body)
        self.assertLessEqual(len(body), submit_proofread.GITHUB_BODY_LIMIT)

    def test_parse_tracker_issue_links_both_pulls_and_posts_complete_body(self):
        request = {
            "locator": {"title": "【文章待拆分】小报", "page_start": 1, "page_end": 1},
            "source_files": ["one"],
            "articles": [{
                "title": "第一篇", "authors": ["作者甲"], "dates": [{"year": 1967}],
                "page_start": 1, "page_end": 1, "content": "完整正文", "base_part_count": 2,
                "ocr": {"content_thresholds": [0.1, 0.1, 0.1, 0.1]},
            }],
        }
        pulls = [{"number": 1, "url": "https://example/config"}, {"number": 2, "url": "https://example/patch"}]
        assets = [{"article_index": 0, "title": "第一篇", "pages": [1], "url": "https://example/crop.webp"}]

        def response(_token, method, path, _expected, payload=None):
            if method == "POST" and path.endswith("/issues"):
                self.assertIn("banned-historical-archives25#1", payload["body"])
                self.assertIn("banned-historical-archives25#2", payload["body"])
                self.assertIn("https://example/crop.webp", payload["body"])
                return {"number": 9, "html_url": "https://example/issue"}
            if method == "POST" and path.endswith("/comments"):
                self.assertIn("完整正文", payload["body"])
                self.assertIn("https://example/crop.webp", payload["body"])
                return {"id": 1}
            return {}

        with patch.object(submit_proofread, "ensure_tracker_label"), \
                patch.object(submit_proofread, "find_tracker_issue", return_value=None), \
                patch.object(submit_proofread, "existing_comment_bodies", return_value=[]), \
                patch.object(submit_proofread, "response_or_fail", side_effect=response):
            result = submit_proofread.upsert_parse_tracker_issue(
                "token", "correction", request, "banned-historical-archives25", "publication", pulls, assets,
            )
        self.assertEqual(result, "https://example/issue")

    def test_identity_change_copies_existing_patch_to_new_article_id(self):
        request = {
            "kind": "proofread",
            "archive_id": 3,
            "article_id": "oldid",
            "publication_id": "publication",
            "metadata": {"article": {"title": "新标题"}},
            "patch": {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}, "description": ""},
        }
        submitted = []

        def get_file(_token, _repo, branch, path):
            if branch == "config":
                return "export default {};", "config-sha"
            if "[newid]" in path:
                return "", None
            if "[oldid]" in path:
                return "export default [\n  {\"version\":1},\n];", "old-patch-sha"
            raise AssertionError((branch, path))

        def submit_file(_token, _repo, base, path, content, *_args):
            submitted.append((base, path, content))
            return {"number": len(submitted), "url": f"https://example.test/{base}", "sha": "a" * 40}

        with patch.dict(os.environ, {"GH_PAT": "token"}), \
                patch.object(submit_proofread, "load_request", return_value=request), \
                patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "update_config", return_value=("updated config", "newid")), \
                patch.object(submit_proofread, "submit_file", side_effect=submit_file), \
                redirect_stdout(io.StringIO()):
            submit_proofread.main()

        self.assertEqual([item[0] for item in submitted], ["config", "ocr_patch"])
        self.assertIn("[newid][publication].ts", submitted[1][1])
        self.assertIn('{"version":1}', submitted[1][2])
        self.assertIn('"version": 2', submitted[1][2])

    def test_metadata_only_identity_change_does_not_create_empty_patch_pull(self):
        request = {
            "kind": "proofread", "archive_id": 3, "article_id": "oldid",
            "publication_id": "publication", "metadata": {"article": {"authors": []}},
        }
        submitted = []

        def get_file(_token, _repo, branch, path):
            if branch == "config":
                return "export default {};", "config-sha"
            self.assertEqual(branch, "ocr_patch")
            return "", None

        def submit_file(_token, _repo, base, path, content, *_args):
            submitted.append((base, path, content))
            return {"number": len(submitted), "url": f"https://example.test/{base}", "sha": "a" * 40}

        with patch.dict(os.environ, {"GH_PAT": "token"}), \
                patch.object(submit_proofread, "load_request", return_value=request), \
                patch.object(submit_proofread, "get_file", side_effect=get_file), \
                patch.object(submit_proofread, "update_config", return_value=("updated config", "newid")), \
                patch.object(submit_proofread, "submit_file", side_effect=submit_file), \
                redirect_stdout(io.StringIO()):
            submit_proofread.main()

        self.assertEqual([(base, path) for base, path, _content in submitted], [("config", "publication.ts")])


class ForkSyncTests(unittest.TestCase):
    def test_mirror_branch_ahead_of_upstream_is_preserved(self):
        def sha(_token, owner, _repo, _branch):
            return "a" * 40 if owner == fork_repositories.UPSTREAM_OWNER else "b" * 40

        with patch.object(fork_repositories, "branch_sha", side_effect=sha), \
                patch.object(fork_repositories, "api_request", return_value=(200, {"status": "ahead"})) as api:
            fork_repositories.sync_branch("token", "banned-historical-archives0", "config")

        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[1], "GET")


class UpstreamPublishTests(unittest.TestCase):
    def _pull(self, number, base_sha, merge_sha, merged_at):
        return {
            "repo": "banned-historical-archives0", "number": number,
            "base": {"ref": "ocr_patch", "sha": base_sha},
            "merge_commit_sha": merge_sha, "merged_at": merged_at,
        }

    def test_proofreading_pulls_uses_merged_at_instead_of_list_merged_field(self):
        closed = {"head": {"repo": {"full_name": "anftm/banned-historical-archives0"}, "ref": "proofread/x-config"}, "base": {"ref": "config"}}
        legacy = {"head": {"repo": {"full_name": "anftm/banned-historical-archives0"}, "ref": "proofread/x-ocr_config"}, "base": {"ref": "ocr_config"}}
        database = {"head": {"repo": {"full_name": "anftm/banned-historical-archives0"}, "ref": "proofread/x-origin"}, "base": {"ref": "origin"}}
        structured = {"head": {"repo": {"full_name": "anftm/banned-historical-archives0"}, "ref": "proofread/x-main"}, "base": {"ref": "main"}}
        pulls = [
            {"number": 1, "merged_at": "2026-08-05T01:00:00Z", **closed},
            {"number": 2, "merged_at": None, **closed},
            {"number": 3, "merged_at": "2026-08-05T02:00:00Z", **closed},
            {"number": 4, "merged_at": "2026-08-05T03:00:00Z", "head": {"repo": {"full_name": "other/not-the-mirror"}, "ref": "proofread/x-config"}, "base": {"ref": "config"}},
            {"number": 5, "merged_at": "2026-08-05T04:00:00Z", "head": {"repo": {"full_name": "anftm/banned-historical-archives0"}, "ref": "feature/x"}, "base": {"ref": "config"}},
            {"number": 6, "merged_at": "2026-08-05T05:00:00Z", **closed, "base": {"ref": "selected"}},
            {"number": 7, "merged_at": "2026-08-05T06:00:00Z", **closed, "head": {"repo": {"full_name": "anftm/banned-historical-archives0"}, "ref": "revert-1-proofread-x-config"}},
            {"number": 8, "merged_at": "2026-08-05T07:00:00Z", **legacy},
            {"number": 9, "merged_at": "2026-08-05T08:00:00Z", **database},
            {"number": 10, "merged_at": "2026-08-05T09:00:00Z", **structured},
        ]
        with patch.object(publish_proofread_upstream, "list_closed_pulls", return_value=pulls), \
                patch.object(publish_proofread_upstream, "MIRROR_OWNER", "anftm"):
            result = publish_proofread_upstream.proofreading_pulls("token", "banned-historical-archives0")
        self.assertEqual([p["number"] for p in result], [1, 3, 7, 8, 9, 10])
        self.assertEqual(next(pull for pull in result if pull["number"] == 7)["reverts"], 1)

    def test_merged_reverted_numbers_recognizes_merged_revert_branches(self):
        head = lambda ref: {"repo": {"full_name": "anftm/banned-historical-archives0"}, "ref": ref}
        pulls = [
            {"number": 20, "merged_at": "2026-08-05T01:00:00Z", "head": head("revert-5-proofread-x-config"), "base": {"ref": "config"}},
            {"number": 21, "merged_at": None, "head": head("revert-6-proofread-x-config"), "base": {"ref": "config"}},
            {"number": 22, "merged_at": "2026-08-05T02:00:00Z", "head": head("proofread/x-config"), "base": {"ref": "config"}},
        ]
        with patch.object(publish_proofread_upstream, "list_closed_pulls", return_value=pulls):
            reverted = publish_proofread_upstream.merged_reverted_numbers("token", "banned-historical-archives0")
        self.assertEqual(reverted, {5})

    def test_filter_reverted_candidates_skips_corrections_with_merged_revert(self):
        candidates = [
            {"repo": "banned-historical-archives0", "number": 5},
            {"repo": "banned-historical-archives0", "number": 6},
        ]
        with patch.object(
            publish_proofread_upstream, "merged_reverted_numbers", return_value={5}
        ) as scan:
            kept, skipped = publish_proofread_upstream.filter_reverted_candidates("token", candidates)
        self.assertEqual([p["number"] for p in kept], [6])
        self.assertEqual(skipped, ["banned-historical-archives0#5"])
        scan.assert_called_once_with("token", "banned-historical-archives0")

    def test_revert_is_published_only_after_original_was_published(self):
        original = {"repo": "banned-historical-archives0", "number": 5}
        revert = {"repo": "banned-historical-archives0", "number": 20, "reverts": 5}
        with patch.object(publish_proofread_upstream, "merged_reverted_numbers", return_value={5}):
            kept, skipped = publish_proofread_upstream.filter_reverted_candidates(
                "token", [original, revert], {"banned-historical-archives0#5"},
            )
        self.assertEqual([pull["number"] for pull in kept], [20])
        self.assertEqual(skipped, ["banned-historical-archives0#5"])

    def test_unpublished_correction_and_revert_are_both_skipped(self):
        original = {"repo": "banned-historical-archives0", "number": 5}
        revert = {"repo": "banned-historical-archives0", "number": 20, "reverts": 5}
        with patch.object(publish_proofread_upstream, "merged_reverted_numbers", return_value={5}):
            kept, skipped = publish_proofread_upstream.filter_reverted_candidates("token", [original, revert])
        self.assertEqual(kept, [])
        self.assertEqual(skipped, ["banned-historical-archives0#5", "banned-historical-archives0#20"])

    def test_publish_group_maps_legacy_path_into_temporary_upstream_branch(self):
        pulls = [self._pull(1, "base-1", "merge-1", "2026-08-03T01:00:00Z")]
        def response(token, method, path, expected, payload=None):
            if method == "POST" and path.endswith("/pulls"):
                return {"number": 8, "html_url": "https://github.com/banned-historical-archives/banned-historical-archives0/pull/8"}
            return {}

        with patch.object(publish_proofread_upstream, "branch_sha", side_effect=["upstream-sha", "mirror-sha", None]), \
                patch.object(publish_proofread_upstream, "open_upstream_pull", return_value=None), \
                patch.object(publish_proofread_upstream, "pull_files", return_value=["archives0/[article][book].ts"]), \
                patch.object(publish_proofread_upstream, "get_file", side_effect=[("content", None), ("", None)]) as get_file, \
                patch.object(publish_proofread_upstream, "put_file") as put_file, \
                patch.object(publish_proofread_upstream, "pull_change_details", return_value=None), \
                patch.object(publish_proofread_upstream, "response_or_fail", side_effect=response) as request_api:
            pull, head = publish_proofread_upstream.publish_group(
                "token", "banned-historical-archives0", "ocr_patch", pulls,
            )

        self.assertEqual(pull["number"], 8)
        self.assertTrue(head.startswith("proofread-upstream-banned-historical-archives0-ocr_patch-"))
        put_file.assert_called_once()
        self.assertEqual(put_file.call_args.args[4], "[article][book].ts")
        self.assertEqual(request_api.call_args.args[1:4], (
            "POST", "/repos/banned-historical-archives/banned-historical-archives0/pulls", (201,),
        ))
        payload = request_api.call_args.args[4]
        self.assertEqual(payload["head"], f"anftm:{head}")
        self.assertEqual(payload["base"], "ocr_patch")
        self.assertEqual(get_file.call_args_list[0].args[3], "merge-1")

    def test_pull_files_accepts_paginated_parse_batch(self):
        first = [{"filename": f"archives25/file-{index}.ts"} for index in range(100)]
        second = [{"filename": "archives25/file-100.ts"}]
        with patch.object(publish_proofread_upstream, "api_request", side_effect=[(200, first), (200, second)]):
            paths = publish_proofread_upstream.pull_files("token", "banned-historical-archives25", 7)
        self.assertEqual(len(paths), 101)
        self.assertEqual(paths[-1], "archives25/file-100.ts")

    def test_publish_group_reuses_existing_batch_branch_pull(self):
        pull = self._pull(1, "base-1", "merge-1", "2026-08-03T01:00:00Z")
        existing = {"number": 8, "body": "<!-- proofreading-upstream-batch -->"}
        with patch.object(publish_proofread_upstream, "branch_sha", side_effect=["upstream-sha", "mirror-sha", "batch-sha"]), \
                patch.object(publish_proofread_upstream, "open_upstream_pull", return_value=existing), \
                patch.object(publish_proofread_upstream, "response_or_fail") as request_api:
            result, branch = publish_proofread_upstream.publish_group(
                "token", "banned-historical-archives0", "ocr_patch", [pull],
            )
        self.assertEqual(result, existing)
        self.assertTrue(branch.startswith("proofread-upstream-"))
        request_api.assert_not_called()

    def test_refresh_claims_releases_legacy_pipeline_target(self):
        state = {
            "claimed": {
                "banned-historical-archives0#1": {
                    "upstream_number": 8,
                    "upstream_url": "https://github.com/anftm/pipeline/pull/8",
                },
            },
        }
        with patch.object(publish_proofread_upstream, "cleanup_legacy_claim", return_value=True) as cleanup, \
                patch.object(publish_proofread_upstream, "api_request") as api:
            changed = publish_proofread_upstream.refresh_claims("token", state)
        self.assertTrue(changed)
        self.assertEqual(state["claimed"], {})
        cleanup.assert_called_once()
        api.assert_not_called()

    def test_cleanup_legacy_claim_closes_pull_and_deletes_branch(self):
        pull = {
            "state": "open",
            "body": "<!-- proofreading-upstream-batch -->",
            "head": {
                "ref": "proofread-upstream-banned-historical-archives0-ocr_patch-deadbeef",
                "repo": {"full_name": "anftm/pipeline"},
            },
        }
        responses = iter([(200, pull), (204, {})])
        with patch.object(publish_proofread_upstream, "api_request", side_effect=lambda *args: next(responses)) as api, \
                patch.object(publish_proofread_upstream, "response_or_fail") as request_api:
            cleaned = publish_proofread_upstream.cleanup_legacy_claim("token", {
                "upstream_repository": "anftm/pipeline", "upstream_number": 14,
            })
        self.assertTrue(cleaned)
        request_api.assert_called_once_with(
            "token", "PATCH", "/repos/anftm/pipeline/pulls/14", (200,), {"state": "closed"},
        )
        self.assertEqual(api.call_args.args[1], "DELETE")
        self.assertIn("proofread-upstream-banned-historical-archives0-ocr_patch-deadbeef", api.call_args.args[2])

    def test_refresh_claims_retires_merged_batch_with_one_api_request(self):
        state = {
            "baseline": [],
            "claimed": {
                "banned-historical-archives0#1": {
                    "upstream_repository": "banned-historical-archives/banned-historical-archives0",
                    "upstream_number": 8,
                },
                "banned-historical-archives0#2": {
                    "upstream_repository": "banned-historical-archives/banned-historical-archives0",
                    "upstream_number": 8,
                },
            },
        }
        with patch.object(publish_proofread_upstream, "api_request", return_value=(200, {"merged": True})) as api:
            changed = publish_proofread_upstream.refresh_claims("token", state)
        self.assertTrue(changed)
        self.assertEqual(state["claimed"], {})
        self.assertEqual(state["baseline"], ["banned-historical-archives0#1", "banned-historical-archives0#2"])
        self.assertEqual(state["published"], ["banned-historical-archives0#1", "banned-historical-archives0#2"])
        api.assert_called_once()

    def test_batch_body_lists_source_pulls_with_marker(self):
        pulls = [self._pull(1, "base-1", "merge-1", "2026-08-03T01:00:00Z")]
        body, overflow = publish_proofread_upstream.split_batch_body("banned-historical-archives0", "ocr_patch", pulls, [None])
        self.assertEqual(overflow, [])
        self.assertIn("<!-- proofreading-upstream-batch -->", body)
        self.assertIn("<!-- proofreading-prs:[{\"repo\":\"banned-historical-archives0\",\"number\":1", body)
        self.assertIn("- [x] [banned-historical-archives0#1]", body)
        self.assertIn("目标仓库：`banned-historical-archives/banned-historical-archives0`", body)
        self.assertIn("目标分支：`ocr_patch`", body)
        self.assertIn("请在目标仓库审核后使用 merge commit 合并", body)

    def test_batch_body_embeds_readable_change_details(self):
        pull = self._pull(1, "base-1", "merge-1", "2026-08-03T01:00:00Z")
        detail = "## 修改内容\n\n- 标题：旧标题 → 新标题"
        body, overflow = publish_proofread_upstream.split_batch_body(
            "banned-historical-archives0", "ocr_patch", [pull], [detail],
        )
        self.assertEqual(overflow, [])
        self.assertIn("## 来源 PR：[banned-historical-archives0#1]", body)
        self.assertIn("- 标题：旧标题 → 新标题", body)

    def test_split_batch_body_moves_overflow_sections_to_comments(self):
        pulls = [
            self._pull(1, "base-1", "merge-1", "2026-08-03T01:00:00Z"),
            self._pull(2, "base-2", "merge-2", "2026-08-03T02:00:00Z"),
        ]
        body, overflow = publish_proofread_upstream.split_batch_body(
            "banned-historical-archives0", "ocr_patch", pulls, ["x" * 5000, "y"], limit=2000,
        )
        self.assertEqual(len(overflow), 1)
        self.assertIn("x" * 5000, overflow[0])
        self.assertIn("其余校订明细见下方评论。", body)
        self.assertNotIn("x" * 5000, body)
        self.assertIn("banned-historical-archives0#2", body)

    def test_pull_change_details_uses_readable_pull_body(self):
        pull = {
            **self._pull(1, "base-1", "merge-1", "2026-08-03T01:00:00Z"),
            "body": "<!-- proofreading:abc -->\n## 校订\n\n- 修改：标题\n\n## 修改内容\n\n- 标题：旧标题 → 新标题\n\n## 原全文\n\n```text\n长文\n```",
        }
        with patch.object(publish_proofread_upstream, "api_request") as api:
            section = publish_proofread_upstream.pull_change_details("token", "banned-historical-archives0", pull)
        api.assert_not_called()
        self.assertIn("## 修改内容", section)
        self.assertIn("- 标题：旧标题 → 新标题", section)
        self.assertIn("## 原全文", section)
        self.assertIn("长文", section)
        self.assertNotIn("<!-- proofreading:abc -->", section)

    def test_pull_change_details_falls_back_to_tracker_issue(self):
        pull = {**self._pull(1, "base-1", "merge-1", "2026-08-03T01:00:00Z"), "body": "由 BHA 校订后端提交。"}
        issue = {"body": '<!-- proofreading:correction -->\n<!-- proofreading-prs:[{"repo":"banned-historical-archives0","number":1,"url":"u"}] -->\n## 校订审核\n\n- 文章：旧\n- 修改：标题\n\n## 修改内容\n\n- 标题：旧标题 → 新标题\n\n## 原全文\n\n```text\n长文\n```\n\n## 审核方式\n\n1. 评论 `/approve`'}
        with patch.object(publish_proofread_upstream, "api_request", return_value=(200, [issue])):
            section = publish_proofread_upstream.pull_change_details("token", "banned-historical-archives0", pull)
        self.assertIsNotNone(section)
        self.assertIn("## 修改内容", section)
        self.assertIn("- 标题：旧标题 → 新标题", section)
        self.assertNotIn("## 审核方式", section)
        self.assertIn("## 原全文", section)
        self.assertIn("长文", section)
        self.assertNotIn("<!-- proofreading:", section)

    def test_post_batch_comments_chunks_oversized_sections(self):
        text = "\n".join(f"行{i}" for i in range(30000))
        calls = []

        def response_or_fail(token, method, path, expected, payload=None):
            calls.append(payload["body"])
            return {"number": 1}

        with patch.object(publish_proofread_upstream, "response_or_fail", side_effect=response_or_fail):
            publish_proofread_upstream.post_batch_comments("token", "anftm", "pipeline", 9, [text])
        self.assertEqual("".join(calls), text)
        self.assertGreater(len(calls), 1)
        for call in calls:
            self.assertLessEqual(len(call), 60000)

    def test_chunk_lines_splits_one_oversized_line(self):
        text = "字" * 120001
        chunks = publish_proofread_upstream.chunk_lines(text, 60000)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 60000 for chunk in chunks))

    def test_upstream_path_strips_archive_prefix(self):
        self.assertEqual(
            publish_proofread_upstream.upstream_path("banned-historical-archives0", "archives0/[a][b].ts"),
            "[a][b].ts",
        )
        self.assertEqual(
            publish_proofread_upstream.upstream_path("banned-historical-archives0", "[a][b].ts"),
            "[a][b].ts",
        )


class TrackerRefreshTests(unittest.TestCase):
    def test_resolved_pull_closes_tracker_issue(self):
        issue = {
            "number": 9,
            "body": '<!-- proofreading-prs:[{"repo":"banned-historical-archives3","number":5,"url":"x"}] -->\n- [ ] [banned-historical-archives3#5](x)',
        }
        with patch.object(update_proofread_issues, "pull_status", return_value={"resolved": True, "merged": True}), \
                patch.object(update_proofread_issues, "response_or_fail") as request_api:
            closed = update_proofread_issues.refresh_issue("token", issue)
        self.assertTrue(closed)
        payload = request_api.call_args.args[4]
        self.assertEqual(payload["state"], "closed")
        self.assertIn("- [x] [banned-historical-archives3#5]", payload["body"])


if __name__ == "__main__":
    unittest.main()
