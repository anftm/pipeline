"""No-network tests of minimal changes; real entrypoint remains paused."""

import copy
import io
import json
import os
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import URLError

import yaml

from scripts import submit_proofread as worker


ROOT = Path(__file__).resolve().parents[1]


class BaselineResponse:
    def __init__(self, value):
        self.raw = json.dumps(value, ensure_ascii=False).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self, limit=-1):
        return self.raw[:limit] if limit >= 0 else self.raw


class MinimalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.request = {"kind": "proofread", "archive_id": 3, "article_id": "a",
                        "publication_id": "p", "doc_id": "3:a:p", "base_content_hash": "b" * 64,
                        "body": {"patch": {"version": 2, "parts": {"0": {"diff": "=1\t-1\t+q\t=1"}},
                                           "comments": {}}}}
        self.preview = {"archive_id": 3, "article_id": "a", "publication_id": "p",
                        "doc_id": "3:a:p", "content_hash": "b" * 64,
                        "article": {"parts": [{"text": "ABC"}], "comments": []}, "source_files": []}

    def test_matching_preview_is_used_for_rendering_without_second_network_read(self):
        with patch.object(worker.urllib.request, "urlopen", return_value=BaselineResponse(self.preview)) as call:
            preview = worker.validate_bha_baseline(self.request)
            worker.fetch_bha_changes(self.request, validated_preview=preview)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.request["fulltext"]["edited"], "AqC")
        self.assertIn("/api/preview/3%3Aa%3Ap", call.call_args.args[0].full_url)

    def test_stale_equal_length_preview_fails_before_external_write(self):
        stale = {**self.preview, "content_hash": "c" * 64,
                 "article": {"parts": [{"text": "XYZ"}], "comments": []}}
        with patch.object(worker.urllib.request, "urlopen", return_value=BaselineResponse(stale)):
            with self.assertRaisesRegex(RuntimeError, "changed after submission"):
                worker.validate_bha_baseline(self.request)

    def test_malformed_identity_and_hash_never_fetch(self):
        for changes in ({"base_content_hash": None}, {"archive_id": True}, {"doc_id": ""},
                        {"article_id": None}, {"publication_id": ""}):
            with self.subTest(changes=changes), patch.object(worker.urllib.request, "urlopen") as read:
                with self.assertRaises(RuntimeError):
                    worker.validate_bha_baseline({**self.request, **changes})
                read.assert_not_called()

    def test_response_identity_mismatch_and_network_failure_fail_closed(self):
        for key, value in (("doc_id", "other"), ("archive_id", True), ("publication_id", "other"),
                           ("article_id", "other"), ("content_hash", "invalid")):
            with self.subTest(key=key), patch.object(worker.urllib.request, "urlopen", return_value=BaselineResponse({**self.preview, key: value})):
                with self.assertRaises(RuntimeError):
                    worker.validate_bha_baseline(self.request)
        with patch.object(worker.urllib.request, "urlopen", side_effect=URLError("synthetic")):
            with self.assertRaisesRegex(RuntimeError, "cannot verify"):
                worker.validate_bha_baseline(self.request)

    def test_parse_requires_archive_25_and_matching_hash(self):
        request = {**self.request, "kind": "parse", "archive_id": 25}
        preview = {**self.preview, "archive_id": 25}
        with patch.object(worker.urllib.request, "urlopen", return_value=BaselineResponse(preview)):
            self.assertEqual(worker.validate_bha_baseline(request), preview)
        with patch.object(worker.urllib.request, "urlopen") as read:
            with self.assertRaises(RuntimeError):
                worker.validate_bha_baseline({**request, "archive_id": 3})
            read.assert_not_called()

    def test_main_checks_baseline_before_any_downstream_git_or_crop_effect(self):
        for kind in ("proofread", "parse"):
            request = {**self.request, "kind": kind, "archive_id": 25 if kind == "parse" else 3}
            with self.subTest(kind=kind), patch.object(worker, "require_paused_delivery"), \
                    patch.dict(os.environ, {"GH_PAT": "synthetic", "GITHUB_EVENT_NAME": "repository_dispatch"}), \
                    patch.object(worker, "load_request", return_value=request), \
                    patch.object(worker, "validate_bha_baseline", side_effect=RuntimeError("stale")), \
                    patch.object(worker, "get_file", side_effect=AssertionError("write path forbidden")) as get_file, \
                    patch.object(worker, "build_review_crops", side_effect=AssertionError("crop forbidden")) as crop, \
                    patch.object(worker, "submit_file", side_effect=AssertionError("PR forbidden")) as submit:
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    worker.main()
                get_file.assert_not_called()
                crop.assert_not_called()
                submit.assert_not_called()

    def test_empty_doc_id_cannot_skip_baseline_check(self):
        for request in ({**self.request, "doc_id": ""}, {key: value for key, value in self.request.items() if key != "doc_id"}):
            with self.subTest(request=request), patch.object(worker, "require_paused_delivery"), \
                    patch.dict(os.environ, {"GH_PAT": "synthetic", "GITHUB_EVENT_NAME": "repository_dispatch"}), \
                    patch.object(worker, "load_request", return_value=request), \
                    patch.object(worker, "submit_file", side_effect=AssertionError("PR forbidden")) as submit:
                with self.assertRaisesRegex(RuntimeError, "baseline identity"):
                    worker.main()
                submit.assert_not_called()

    def test_bha_proofread_without_tracker_stops_before_archive_effects(self):
        for token in (None, "", "  "):
            env = {"GH_PAT": "synthetic", "GITHUB_EVENT_NAME": "repository_dispatch"}
            if token is not None:
                env["TRACKER_TOKEN"] = token
            with self.subTest(token=token), patch.dict(os.environ, env, clear=True), \
                    patch.object(worker, "require_paused_delivery"), \
                    patch.object(worker, "load_request", return_value=self.request), \
                    patch.object(worker, "validate_bha_baseline", return_value=self.preview), \
                    patch.object(worker, "capture_submission_bases") as capture, \
                    patch.object(worker, "submit_file") as submit:
                with self.assertRaisesRegex(RuntimeError, "TRACKER_TOKEN is required"):
                    worker.main()
                capture.assert_not_called()
                submit.assert_not_called()

    def test_old_auto_merge_flag_never_merges_even_when_tracker_reply_is_missing(self):
        request = copy.deepcopy(self.request)
        request.update(auto_merge=True, auto_merge_policy=dict(worker.AUTO_MERGE_POLICY))
        with patch.dict(os.environ, {"GH_PAT": "synthetic", "TRACKER_TOKEN": "synthetic",
                                     "GITHUB_EVENT_NAME": "repository_dispatch"}, clear=True), \
                patch.object(worker, "require_paused_delivery"), \
                patch.object(worker, "load_request", return_value=request), \
                patch.object(worker, "validate_bha_baseline", return_value=self.preview), \
                patch.object(worker, "capture_submission_bases", return_value={}), \
                patch.object(worker, "get_file", return_value=("", None)), \
                patch.object(worker, "submit_file", return_value={"number": 1, "url": "fixture/pr"}) as submit, \
                patch.object(worker, "upsert_tracker_issue", return_value=None) as tracker, \
                patch.object(worker, "merge_pull") as merge, \
                patch.object(worker, "delete_pull_branch") as delete, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "tracker response is unconfirmed"):
                worker.main()
        submit.assert_called_once()
        tracker.assert_called_once()
        merge.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_base_change_before_branch_creation_blocks_external_write(self):
        expected = {"config": "a" * 40, "ocr_patch": "b" * 40}
        def branch(_token, _repo, name):
            if name.startswith("proofread/"):
                return None
            return "c" * 40 if name == "config" else "b" * 40
        with patch.object(worker, "open_pull_request"), patch.object(worker, "branch_sha", side_effect=branch), \
                patch.object(worker, "get_file", return_value=("old", "d" * 40)), \
                patch.object(worker, "response_or_fail", side_effect=AssertionError("write forbidden")) as write:
            with self.assertRaisesRegex(RuntimeError, "base branch changed"):
                worker.submit_file("synthetic", "repo", "config", "p.ts", "new", "title", "body", "123",
                                   expected_bases=expected)
            write.assert_not_called()

    def test_absent_optional_ocr_config_is_recorded_and_must_remain_absent(self):
        current = {"main": "a" * 40, "config": "b" * 40, "ocr_config": None, "ocr_patch": "c" * 40}
        def branch(_token, _repo, name):
            return current[name]
        with patch.object(worker, "branch_sha", side_effect=branch):
            expected = worker.capture_submission_bases("synthetic", "repo", "proofread")
            worker.verify_submission_bases("synthetic", "repo", expected)
            current["ocr_config"] = "d" * 40
            with self.assertRaisesRegex(RuntimeError, "base branch changed"):
                worker.verify_submission_bases("synthetic", "repo", expected)

    def test_base_change_after_branch_write_blocks_pr_creation(self):
        expected = {"config": "a" * 40}
        state = {"writes": 0}
        def branch(_token, _repo, name):
            if name.startswith("proofread/"):
                return None
            return "a" * 40 if state["writes"] < 2 else "c" * 40
        def write(_token, method, path, *_args, **_kwargs):
            self.assertFalse(path.endswith("/pulls"))
            self.assertIn(method, ("POST", "PUT"))
            state["writes"] += 1
            return {}
        with patch.object(worker, "open_pull_request"), patch.object(worker, "branch_sha", side_effect=branch), \
                patch.object(worker, "get_file", return_value=("old", "d" * 40)), \
                patch.object(worker, "response_or_fail", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "base branch changed"):
                worker.submit_file("synthetic", "repo", "config", "p.ts", "new", "title", "body", "123",
                                   expected_bases=expected)
        self.assertEqual(state["writes"], 2)

    def test_workflows_keep_events_and_allow_multiple_pending(self):
        for name, event in (("proofread-submit.yml", "submit-proofread"),
                            ("proofread-submit-parse.yml", "submit-parse")):
            workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(workflow["on"]["repository_dispatch"]["types"], [event])
            self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
            self.assertEqual(workflow["concurrency"]["queue"], "max")

    def test_comment_listing_requires_complete_well_formed_pages(self):
        responses = [(200, [{"body": "fixture"}] * 100)] * 10
        for listing in (responses, [(200, [None])], [(200, [{}])], [(200, [{"body": 5}])], [(403, [])]):
            with self.subTest(listing=listing[:1]), patch.object(worker, "api_request", side_effect=listing):
                with self.assertRaisesRegex(RuntimeError, "listing"):
                    worker.existing_comment_bodies("synthetic", 9)
        with patch.object(worker, "api_request", side_effect=[(200, [{"body": "first"}] * 100),
                                                                (200, [{"body": None}, {"body": "last"}])]):
            bodies = worker.existing_comment_bodies("synthetic", 9)
        self.assertEqual((len(bodies), bodies[-2:]), (102, ["", "last"]))

    def test_comment_marker_does_not_accept_conflicts_or_duplicates(self):
        marker, body = "<!-- synthetic -->", "<!-- synthetic -->\nexpected"
        self.assertFalse(worker.exact_comment_present([], marker, body))
        self.assertTrue(worker.exact_comment_present([body], marker, body))
        for values in ([marker + "\nwrong"], [body, body], [body, marker + "\nwrong"]):
            with self.subTest(values=values), self.assertRaisesRegex(RuntimeError, "recovery review"):
                worker.exact_comment_present(values, marker, body)

    def test_later_comment_conflict_is_found_before_first_comment_post(self):
        request = {"fulltext": {"original": "old", "edited": "new"}}
        comments = worker.fulltext_comment_bodies(request, "proofreading-fulltext:abc")
        self.assertEqual(len(comments), 2)
        later_collision = comments[1][0] + "\nwrong"
        with patch.object(worker, "existing_comment_bodies", return_value=[later_collision]), \
                patch.object(worker, "response_or_fail") as post:
            with self.assertRaisesRegex(RuntimeError, "recovery review"):
                worker.post_fulltext_comments("synthetic", 9, request, "proofreading-fulltext:abc")
        post.assert_not_called()

    def test_proofread_fulltext_comment_marker_collision_stops_without_post(self):
        request = {"fulltext": {"original": "old", "edited": "new"}}
        marker, _body = worker.fulltext_comment_bodies(request, "proofreading-fulltext:abc")[0]
        with patch.object(worker, "existing_comment_bodies", return_value=[marker + "\nwrong"]), \
                patch.object(worker, "response_or_fail") as post:
            with self.assertRaisesRegex(RuntimeError, "recovery review"):
                worker.post_fulltext_comments("synthetic", 9, request, "proofreading-fulltext:abc")
        post.assert_not_called()

    def test_parse_tracker_comment_marker_collision_stops_without_comment_write(self):
        request = {"body": {"locator": {"title": "fixture", "page_start": 1, "page_end": 1},
                            "articles": [{"title": "one", "authors": [], "dates": [], "page_start": 1,
                                          "page_end": 1, "content": "text", "base_part_count": 1}]}}
        marker, _body = worker.parse_article_comment_bodies(request, "abc", [])[0]
        calls = []
        def response(_token, method, path, _codes, payload):
            calls.append((method, path))
            if path.endswith("/issues"):
                return {"number": 9, "html_url": "https://example.invalid/issue/9"}
            raise AssertionError("comment write is forbidden")
        with patch.object(worker, "ensure_tracker_label"), patch.object(worker, "find_tracker_issue", return_value=None), \
                patch.object(worker, "existing_comment_bodies", return_value=[marker + "\nwrong"]), \
                patch.object(worker, "response_or_fail", side_effect=response):
            with self.assertRaisesRegex(RuntimeError, "recovery review"):
                worker.upsert_parse_tracker_issue("synthetic", "abc", request, "repo", "publication",
                                                 [{"number": 1, "url": "u1"}, {"number": 2, "url": "u2"}], [])
        self.assertEqual(len(calls), 1)  # issue creation already occurred; manual recovery remains necessary

    def test_tracker_issue_lookup_rejects_truncation_and_duplicate_markers(self):
        marker = "<!-- proofreading:abc -->"
        issue = {"number": 1, "body": marker}
        for pages in ([(200, [issue, {**issue, "number": 2}])],
                      [(200, [{"number": i, "body": "unrelated"} for i in range(100)])] * 10,
                      [(200, [{"number": 1}])], [(200, [{"number": 1, "body": {}}])]):
            with self.subTest(first=pages[0][0]), patch.object(worker, "api_request", side_effect=pages):
                with self.assertRaisesRegex(RuntimeError, "tracker issue"):
                    worker.find_tracker_issue("synthetic", "abc")
        with patch.object(worker, "api_request", return_value=(200, [issue])):
            self.assertEqual(worker.find_tracker_issue("synthetic", "abc"), issue)
        with patch.object(worker, "api_request", return_value=(200, [{"number": 7, "body": None}])):
            self.assertIsNone(worker.find_tracker_issue("synthetic", "abc"))

    def test_existing_tracker_issue_is_not_updated_by_proofread_or_parse(self):
        parse_request = {"body": {"locator": {"title": "fixture", "page_start": 1, "page_end": 1},
                                 "articles": [{"title": "one", "authors": [], "dates": [], "page_start": 1,
                                               "page_end": 1, "content": "text", "base_part_count": 1}]}}
        proofread_request = {"article_id": "one", "changed": []}
        pulls = [{"number": 1, "url": "u1"}, {"number": 2, "url": "u2"}]
        existing = {"number": 9, "body": "<!-- proofreading:abc -->"}
        with patch.object(worker, "ensure_tracker_label"), \
                patch.object(worker, "find_tracker_issue", return_value=existing), \
                patch.object(worker, "response_or_fail") as write:
            with self.assertRaisesRegex(RuntimeError, "separate recovery review"):
                worker.upsert_parse_tracker_issue("synthetic", "abc", parse_request, "repo", "publication", pulls, [])
            with self.assertRaisesRegex(RuntimeError, "separate recovery review"):
                worker.upsert_tracker_issue("synthetic", "abc", proofread_request, "repo", "one", pulls)
        write.assert_not_called()

    def test_real_entrypoint_still_pauses_before_loading_or_effects(self):
        with patch.object(worker, "load_request", side_effect=AssertionError("event must not load")):
            with self.assertRaisesRegex(RuntimeError, "delivery is paused"):
                worker.main()


if __name__ == "__main__":
    unittest.main(verbosity=2)
