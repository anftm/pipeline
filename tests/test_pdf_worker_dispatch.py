import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from scripts import pdf_worker_dispatch as dispatch, pdf_worker_lanes


REPO = "ambrossee768/pdf-pipeline"
CENTRAL = "anftm/pipeline"


def worker(run_id=100, status="completed", conclusion="success", repository=REPO):
    return {"id": run_id, "repository": {"full_name": repository}, "head_branch": "main",
            "path": ".github/workflows/" + dispatch.WORKER, "event": "workflow_dispatch",
            "status": status, "conclusion": conclusion,
            "display_title": "PDF ocr / " + repository.split("/")[0]}


def publisher(run_id=200, source=100, status="in_progress", repository=REPO):
    return {"id": run_id, "repository": {"full_name": CENTRAL}, "head_branch": "main",
            "path": ".github/workflows/" + dispatch.PUBLISHER, "event": "workflow_dispatch",
            "status": status, "display_title": f"PDF publish ocr / {repository} / {source}"}


class Queue:
    def __init__(self):
        self.records = {100: worker(), 101: worker(101), 200: publisher(), 201: publisher(201, 101)}
        self.workers = []
        self.publishers = [self.records[200]]
        self.calls = []
        self.posts = []
        self.visibility_delay = 0

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "POST":
            self.posts.append((path, payload))
            self.workers.append(worker(300, "pending", None))
            return {}
        if "/actions/runs/" in path:
            return self.records[int(path.rsplit("/", 1)[1])]
        if f"/{dispatch.WORKER}/runs?" in path:
            rows = self.workers
            if self.posts and self.visibility_delay:
                self.visibility_delay -= 1
                rows = []
        elif f"/{dispatch.PUBLISHER}/runs?" in path:
            rows = self.publishers
        else:
            raise AssertionError(path)
        status = path.split("status=", 1)[1].split("&", 1)[0]
        page = int(path.rsplit("page=", 1)[1])
        rows = [row for row in rows if row["status"] == status]
        return {"workflow_runs": copy.deepcopy(rows[(page - 1) * 100:page * 100])}


class WorkerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.config = pdf_worker_lanes.load_config()
        self.api = Queue()
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": CENTRAL,
                    "GITHUB_WORKFLOW_REF": CENTRAL + "/.github/workflows/" + dispatch.PUBLISHER + "@refs/heads/main",
                    "GITHUB_RUN_ID": "200"}

    def handoff(self, **options):
        return dispatch.handoff(self.config, REPO, 100, 200, api=self.api, sleep=Mock(), **options)

    def test_dry_run_is_read_only_and_defaults_to_no_dispatch(self):
        report = self.handoff()
        self.assertFalse(report["applied"])
        self.assertEqual(report["decision"], "would-dispatch")
        self.assertEqual(self.api.posts, [])

    def test_every_active_worker_status_blocks_even_when_wrapper_is_only_queued(self):
        for status in dispatch.ACTIVE_STATUSES:
            with self.subTest(status=status):
                self.api.workers = [worker(300, status, None)]
                report = self.handoff()
                self.assertEqual(report["decision"], "deferred")
                self.assertEqual(report["active_workers"], [300])
                self.assertEqual(report["reason"], "worker-already-active")
                self.assertEqual(self.api.posts, [])

    def test_unpublished_completed_worker_also_blocks_until_its_callback_drains(self):
        self.api.publishers.append(publisher(201, 101, "pending"))
        with patch.dict(os.environ, self.env, clear=True):
            report = self.handoff(apply=True)
        self.assertEqual(report["awaiting_publishers"], [201])
        self.assertEqual(report["reason"], "worker-results-await-publication")
        self.assertEqual(self.api.posts, [])
        # The last publisher owns one continuation, not one per completed source.
        self.api.publishers = [publisher(201, 101)]
        with patch.dict(os.environ, {**self.env, "GITHUB_RUN_ID": "201"}, clear=True):
            report = dispatch.handoff(self.config, REPO, 101, 201, api=self.api, apply=True, sleep=Mock())
        self.assertEqual(report["decision"], "dispatch-requested")
        self.assertEqual(len(self.api.posts), 1)

    def test_normal_and_failed_only_publishers_do_not_fork_the_chain(self):
        with patch.dict(os.environ, self.env, clear=True):
            first = self.handoff(apply=True)
        self.assertEqual(first["observed_workers"], [300])
        self.api.publishers = [publisher(201, 101)]
        with patch.dict(os.environ, {**self.env, "GITHUB_RUN_ID": "201"}, clear=True):
            second = dispatch.handoff(self.config, REPO, 101, 201, api=self.api, apply=True, sleep=Mock())
        self.assertEqual(second["decision"], "deferred")
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.api.posts[0], (f"repos/{REPO}/actions/workflows/{dispatch.WORKER}/dispatches",
                                          {"ref": "main", "inputs": {"stage": "auto", "limit": "20"}}))

    def test_other_account_publishers_do_not_occupy_this_account(self):
        self.api.publishers.append(publisher(201, 101, "pending", "vomebook/pdf-pipeline"))
        self.assertEqual(self.handoff()["decision"], "would-dispatch")

    def test_queue_pagination_does_not_miss_the_blocker_on_page_two(self):
        self.api.publishers = [publisher(1000 + n, 100 + n, "pending", "vomebook/pdf-pipeline") for n in range(100)]
        self.api.publishers.append(publisher(9999, 101, "pending"))
        report = self.handoff()
        self.assertEqual(report["awaiting_publishers"], [9999])
        self.assertTrue(any("page=2" in path for _, path, _ in self.api.calls))

    def test_unknown_queue_or_api_error_fails_closed(self):
        self.api.publishers.append({**publisher(201), "display_title": "unrecognized callback"})
        with self.assertRaises(ValueError):
            self.handoff()
        self.assertEqual(self.api.posts, [])
        with self.assertRaises(RuntimeError):
            dispatch.handoff(self.config, REPO, 100, 200, api=Mock(side_effect=RuntimeError("unavailable")))

    def test_source_and_publisher_identity_are_checked_before_admission(self):
        for record_id, field, value in [(100, "status", "in_progress"), (100, "conclusion", "cancelled"),
                                        (200, "head_branch", "topic"), (200, "display_title", "wrong source")]:
            with self.subTest(record_id=record_id, field=field):
                before = self.api.records[record_id][field]
                self.api.records[record_id][field] = value
                with self.assertRaises(ValueError):
                    self.handoff()
                self.api.records[record_id][field] = before
        self.assertEqual(self.api.posts, [])

    def test_apply_requires_the_actual_central_publisher_context(self):
        for change in ({"GITHUB_ACTIONS": "false"}, {"GITHUB_REPOSITORY": "other/pipeline"},
                       {"GITHUB_RUN_ID": "201"}, {"GITHUB_WORKFLOW_REF": "forged"}):
            with patch.dict(os.environ, {**self.env, **change}, clear=True):
                with self.assertRaises(RuntimeError):
                    self.handoff(apply=True)
        self.assertEqual(self.api.posts, [])

    def test_intent_is_saved_and_unknown_post_outcome_is_never_retried(self):
        reports = []
        api = self.api
        def failing_post(method, path, payload=None):
            if method == "POST":
                raise TimeoutError("dispatch response lost")
            return api(method, path, payload)
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(TimeoutError):
                dispatch.handoff(self.config, REPO, 100, 200, apply=True, api=failing_post,
                                 checkpoint=lambda r: reports.append(copy.deepcopy(r)))
        self.assertEqual(reports[-1]["decision"], "dispatch-requesting")

    def test_accepted_run_must_be_visible_before_another_callback_can_enter(self):
        self.api.visibility_delay = len(dispatch.ACTIVE_STATUSES)
        sleep = Mock()
        with patch.dict(os.environ, self.env, clear=True):
            report = dispatch.handoff(self.config, REPO, 100, 200, apply=True, api=self.api, sleep=sleep)
        self.assertEqual(report["observed_workers"], [300])
        sleep.assert_called_once_with(5)
        self.assertEqual(len(self.api.posts), 1)
        self.api = Queue()
        self.api.visibility_delay = 1000
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "inspect it before any retry"):
                dispatch.handoff(self.config, REPO, 100, 200, apply=True, api=self.api, sleep=Mock())
        self.assertEqual(len(self.api.posts), 1)

    def test_workflow_has_one_guarded_handoff_and_retains_the_report(self):
        root = Path(__file__).parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/pdf-account-publish.yml").read_text())
        steps = workflow["jobs"]["publish"]["steps"]
        handoffs = [s for s in steps if "pdf_worker_dispatch.py" in s.get("run", "")]
        self.assertEqual(len(handoffs), 1)
        self.assertIn("inputs.stage == 'render'", handoffs[0]["if"])
        self.assertIn("steps.queue.outputs.shard_count != '0'", handoffs[0]["if"])
        self.assertLess(next(i for i, s in enumerate(steps) if s.get("name") == "Publish Reader sidecar to Pages"),
                        steps.index(handoffs[0]))
        self.assertEqual(workflow["concurrency"]["group"], "reader-sidecar")
        artifact = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
        self.assertIn("output/pdf-ocr/dispatch.json", artifact["with"]["path"])
        self.assertGreater(steps.index(artifact), steps.index(handoffs[0]))


class WatchdogTests(unittest.TestCase):
    def test_only_idle_lanes_are_dispatched(self):
        from datetime import datetime, timezone
        config = pdf_worker_lanes.load_config()
        central = config["publisher_repository"]
        repos = dispatch.lane_repositories(config)
        self.assertGreaterEqual(len(repos), 3)
        busy_worker, pending_pub, idle = repos[:3]
        recent = None
        now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
        posts = []

        def api(method, path, payload=None):
            if method == "POST":
                posts.append((path, payload))
                return {}
            if path.startswith(f"repos/{central}/actions/workflows/{dispatch.PUBLISHER}/runs"):
                rows = [{"id": 9, "status": "queued", "path": ".github/workflows/" + dispatch.PUBLISHER,
                         "repository": {"full_name": central},
                         "display_title": f"PDF publish ocr / {pending_pub} / 5"}] if "status=queued" in path else []
                return {"workflow_runs": rows}
            repo = path.split("/actions/")[0][len("repos/"):]
            if "?status=" in path:
                rows = [{"id": 7, "status": "in_progress", "path": ".github/workflows/" + dispatch.WORKER,
                         "repository": {"full_name": repo}}] if repo == busy_worker and "in_progress" in path else []
                return {"workflow_runs": rows}
            stamp = "2026-10-20T14:50:00Z" if repo == recent else "2026-10-19T13:00:00Z"
            return {"workflow_runs": [{"updated_at": stamp}]}

        report = dispatch.watchdog(config, apply=True, api=api, now=now)
        decisions = {lane["repository"]: lane["decision"] for lane in report["lanes"]}
        self.assertEqual(set(decisions), set(repos))
        self.assertEqual(decisions[pending_pub], "publication-pending")
        dispatched = [path.split("/actions/")[0][len("repos/"):] for path, _ in posts]
        self.assertNotIn(pending_pub, dispatched)
        self.assertTrue(all(payload == {"ref": "main", "inputs": {"stage": "auto", "limit": "20"}}
                            for _, payload in posts))
        for repo, decision in decisions.items():
            self.assertEqual(repo in dispatched, decision == "dispatch-requested")
        self.assertEqual(decisions[busy_worker], "worker-active")
        self.assertEqual(decisions[idle], "dispatch-requested")
        # A worker that finished minutes ago may still be handing off.
        posts.clear()
        recent = idle
        report = dispatch.watchdog(config, apply=True, api=api, now=now)
        self.assertEqual({l["repository"]: l["decision"] for l in report["lanes"]}[idle], "recently-finished")
        self.assertNotIn(idle, [p.split("/actions/")[0][len("repos/"):] for p, _ in posts])


if __name__ == "__main__":
    unittest.main()
