import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch

from scripts import reader_v3_automation as auto, publish_reader_v3 as publication
from tests import test_publish_reader_v3 as fixtures


def entry(key="VoiceOfML/books\0book.pdf", source="a" * 64, count=97):
    return {"key": key, "repo": key.split("\0")[0], "path": key.split("\0")[1],
            "source_kind": "upstream", "source_revision": "b" * 40, "source_sha256": source,
            "source_bytes": 100, "page_count": count, "status": "ready",
            "ocr_manifest": f"objects/{source[:2]}/{source}/{'c' * 16}/ocr-manifest.json",
            "ocr_manifest_sha256": "d" * 64, "ocr_manifest_bytes": 100}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.store = fixtures.Store()
        self.now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        self.add_entries(entry())

    def add_entries(self, *entries):
        self.store.objects[(publication.ASSETS, auto.REGISTRY)] = publication.encode(
            {"version": 1, "files": {e["key"]: e for e in entries}})

    def test_dry_run_never_builds_or_writes(self):
        builder = Mock()
        result = auto.run(self.store, build=builder, now=self.now)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(self.store.writes, [])
        builder.assert_not_called()

    def test_staged_checkpoint_retries_promotion_without_rebuilding(self):
        fixture = fixtures.V3PublicationTests()
        fixture.setUp()
        try:
            ref = fixture.candidate(key=entry()["key"])
            staged = publication.stage(self.store, ref, fixture.bundle, apply=True)
            build = Mock(return_value=staged)
            self.store.fail_pointer = True
            first = auto.run(self.store, apply=True, build=build, now=self.now)
            self.assertEqual(first["processed"][0]["status"], "retry")
            self.store.fail_pointer = False
            second = auto.run(self.store, apply=True, build=build, now=self.now + timedelta(hours=3))
            self.assertEqual(second["processed"][0]["status"], "published")
            self.assertEqual(build.call_count, 1)
            self.assertEqual(second["budget"]["attempts"], 1)
        finally:
            fixture.temp.cleanup()

    def test_interrupted_build_charges_budget_and_daily_reset_is_utc(self):
        build = Mock(side_effect=OSError("transport"))
        auto.run(self.store, apply=True, build=build, now=self.now)
        auto.run(self.store, apply=True, build=build, now=self.now + timedelta(hours=3))
        auto.run(self.store, apply=True, build=build, now=self.now + timedelta(hours=10))
        result = auto.run(self.store, apply=True, build=build, now=self.now + timedelta(hours=12))
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(build.call_count, 3)
        auto.run(self.store, apply=True, build=build, now=self.now + timedelta(days=1))
        task = next(iter(auto.load_state(self.store)["tasks"].values()))
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["attempts"], 3)

    def test_existing_book_is_protected_even_when_raw_recipe_or_source_changes(self):
        fixture = fixtures.V3PublicationTests()
        fixture.setUp()
        try:
            ref = fixture.candidate(key=entry()["key"])
            staged = publication.stage(self.store, ref, fixture.bundle, apply=True)
            publication.promote(self.store, staged["candidate"], None, apply=True)
            self.add_entries(entry(source="e" * 64))
            build = Mock()
            result = auto.run(self.store, apply=True, build=build, now=self.now)
            self.assertEqual(result["eligible"], 0)
            build.assert_not_called()
            self.assertEqual(next(iter(auto.load_state(self.store)["tasks"].values()))["status"], "protected")
        finally:
            fixture.temp.cleanup()

    def test_page_budget_never_truncates_a_book(self):
        self.add_entries(entry(count=auto.DAILY_PAGES + 1))
        result = auto.run(self.store, now=self.now)
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["eligible"], 0)

    def test_preview_admission_does_not_wait_for_ocr_and_ready_specs_keep_identity(self):
        book = entry()
        ready_spec, _ = auto.spec_for(book["key"], book)
        self.assertEqual(ready_spec, {
            "source_key": book["key"], "source_sha256": book["source_sha256"], "vector_previews": True,
            "ocr_manifest": {"bucket": "vomebook/pdf-pages-v2", "path": book["ocr_manifest"],
                             "sha256": book["ocr_manifest_sha256"], "bytes": book["ocr_manifest_bytes"], "role": "provenance"},
            "primary_source": {"repo": book["repo"], "path": book["path"], "revision": book["source_revision"]},
            "primary": {"bucket": "vomebook/pdf-pages-v2", "path": "objects/aa/" + "a" * 64 + "/" +
                        auto.text.digest({"primary": 1})[:16] + "/document.pdf",
                        "sha256": "a" * 64, "bytes": 100, "role": "runtime"}})
        pending = {k: v for k, v in book.items() if not k.startswith("ocr_manifest")}
        self.add_entries({**pending, "status": "rendered"})
        self.store.objects[(publication.ASSETS, auto.RENDER_REGISTRY)] = publication.encode(
            {"version": 1, "files": {book["key"]: book}})
        report = auto.wave(self.store, apply=True, now=self.now, preview_first=True)
        self.assertEqual(len(report["wave"]), 1)
        task = auto.load_state(self.store)["tasks"][report["wave"][0]["identity"]]
        self.assertTrue(task["spec"]["preview_only"])
        self.assertNotIn("ocr_manifest", task["spec"])
        self.assertEqual(task["page_count"], book["page_count"])
        # An older retained OCR route must not be hidden behind an image-only
        # override even if some producer left a non-ready status on the entry.
        for status in ("rendered", "failed"):
            state = {"version": 1, "kind": "reader-v3-automation", "tasks": {}, "days": {}}
            auto.discover(state, {"version": 1, "files": {book["key"]: {**book, "status": status}}},
                          {"version": 1, "files": {book["key"]: book}})
            self.assertEqual(state["tasks"], {})

    def test_preview_ready_book_can_receive_text_but_existing_ready_text_is_protected(self):
        book = entry(count=1)
        self.add_entries({**{k: v for k, v in book.items() if not k.startswith("ocr_manifest")}, "status": "rendered"})
        fixture = fixtures.V3PublicationTests()
        fixture.setUp()
        try:
            preview = fixture.preview_candidate(key=book["key"])
            staged = publication.stage(self.store, preview, fixture.bundle, apply=True)
            publication.promote(self.store, staged["candidate"], None, apply=True)
            self.add_entries(book)
            report = auto.wave(self.store, apply=True, now=self.now, wave_size=1)
            self.assertEqual(len(report["wave"]), 1)
            task_id = report["wave"][0]["identity"]
            self.store.allowed = False
            self.store.assert_candidate_builder = lambda protocol: None
            builder = Mock(return_value=staged)
            with patch.object(publication, "build_stage", builder):
                auto.build_task(self.store, task_id, 1)
            self.assertEqual(builder.call_args.args[1]["reuse_reading"], preview)
            self.assertNotIn("preview_only", builder.call_args.args[1])
        finally:
            fixture.temp.cleanup()

    def test_ocr_finishing_during_preview_build_supersedes_only_the_image_only_task(self):
        book = entry(count=1)
        pending = {**{k: v for k, v in book.items() if not k.startswith("ocr_manifest")}, "status": "rendered"}
        state = {"version": 1, "kind": "reader-v3-automation", "tasks": {}, "days": {}}
        rendered = {"version": 1, "files": {book["key"]: book}}
        auto.discover(state, {"version": 1, "files": {book["key"]: pending}}, rendered)
        identity = next(iter(state["tasks"]))
        state["tasks"][identity].update(status="building", attempts=1)
        auto.discover(state, {"version": 1, "files": {book["key"]: book}}, rendered)
        self.assertEqual(state["tasks"][identity]["status"], "superseded")
        self.assertEqual(sum(t["status"] == "pending" and not t["spec"].get("preview_only") for t in state["tasks"].values()), 1)

    def test_unusable_converted_text_requires_review_without_repeated_builds(self):
        builder = Mock(side_effect=publication.PublicationReviewRequired())
        report = auto.run(self.store, apply=True, build=builder, now=self.now)
        self.assertEqual(report["processed"][0]["status"], "needs-review")
        auto.run(self.store, apply=True, build=builder, now=self.now + timedelta(days=1))
        self.assertEqual(builder.call_count, 1)

    def test_explicit_retry_retains_day_charges_and_failure_history(self):
        auto.run(self.store, apply=True, build=Mock(side_effect=OSError()), now=self.now)
        state = auto.load_state(self.store)
        identity = next(iter(state["tasks"]))
        before = copy.deepcopy(state["days"])
        with patch.dict("os.environ", {"GITHUB_ACTOR": "reviewer"}):
            auto.retry(self.store, {"task_ids": [identity]}, apply=True)
        state = auto.load_state(self.store)
        self.assertEqual(state["days"], before)
        self.assertEqual(state["tasks"][identity]["attempts"], 0)
        self.assertEqual(state["tasks"][identity]["retries"][0]["actor"], "reviewer")

    def test_converted_primary_uses_qualified_derivative_and_original_identity(self):
        value = entry(key="VoiceOfML/books\0book.djvu")
        value.update(source_kind="generated", reader_assets_bucket="vomebook/pdf-pages-v2",
                     reader_assets_path="derived/migration-test/" + "a" * 32 + "/document.pdf", source_url="")
        spec, count = auto.spec_for(value["key"], value)
        self.assertEqual(count, 97)
        self.assertEqual(spec["primary"]["path"], value["reader_assets_path"])
        self.assertNotIn("primary_source", spec)
        for change in ({"source_kind": "upstream"}, {"reader_assets_bucket": "melsm/pdf-archive-v2"},
                       {"reader_assets_path": "derived/../document.pdf"}, {"source_url": "https://evil.example/book.pdf"}):
            with self.assertRaises(ValueError):
                auto.spec_for(value["key"], {**value, **change})


class WaveTests(unittest.TestCase):
    """Parallel builds outside the writer lock; only promotion is serialized."""

    def setUp(self):
        self.store = fixtures.Store()
        self.now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        books = [entry(key=f"VoiceOfML/books\0b{i}.pdf", source=f"{i:x}" * 64, count=10 + i) for i in range(3)]
        self.store.objects[(publication.ASSETS, auto.REGISTRY)] = publication.encode(
            {"version": 1, "files": {e["key"]: e for e in books}})

    def test_dry_run_plans_without_writes_and_apply_charges_a_wave(self):
        plan = auto.wave(self.store, now=self.now, wave_size=2)
        self.assertEqual(len(plan["next"]), 2)
        self.assertEqual(self.store.writes, [])
        report = auto.wave(self.store, apply=True, now=self.now, wave_size=2)
        self.assertEqual([item["attempt"] for item in report["wave"]], [1, 1])
        self.assertEqual(report["budget"]["attempts"], 2)
        self.assertFalse(report["changed"])
        state = auto.load_state(self.store)
        self.assertEqual(sum(t["status"] == "building" for t in state["tasks"].values()), 2)
        # Any unfinished wave blocks the next wave, even for different books.
        again = auto.wave(self.store, apply=True, now=self.now, wave_size=8)
        self.assertEqual(again["wave"], [])
        self.assertTrue(again["admission_blocked"])
        self.assertEqual(again["inflight"], 2)
        self.assertEqual(again["budget"]["attempts"], 2)

    def test_unpublished_candidates_bound_the_next_wave_and_scoped_pilot_is_exact(self):
        report = auto.wave(self.store, apply=True, now=self.now, wave_size=3)
        state = auto.load_state(self.store)
        for item in report["wave"]:
            task = state["tasks"][item["identity"]]
            task.update(status="staged", candidate={"path": "retained"})
        extra = entry(key="VoiceOfML/books\0extra.pdf", source="f" * 64)
        registry = json.loads(self.store.objects[(publication.ASSETS, auto.REGISTRY)])
        registry["files"][extra["key"]] = extra
        self.store.objects[(publication.ASSETS, auto.REGISTRY)] = publication.encode(registry)
        self.store.objects[(publication.ASSETS, auto.STATE)] = publication.encode(state)
        def promote(_store, task):
            task["status"] = "published"
        with patch.object(auto, "_promote", side_effect=promote):
            report = auto.wave(self.store, apply=True, now=self.now, wave_size=3, promotion_limit=1)
        self.assertEqual(len(report["processed"]), 1)
        self.assertEqual(len(report["wave"]), 1)
        state = auto.load_state(self.store)
        self.assertEqual(sum(t["status"] in {"building", "staged"} for t in state["tasks"].values()), 3)
        # A pilot selection cannot dispatch another source, charge it, or
        # supersede its already existing pending task.
        other = fixtures.Store()
        other.objects[(publication.ASSETS, auto.REGISTRY)] = publication.encode(registry)
        scoped = auto.wave(other, apply=True, now=self.now, source_keys=[extra["key"]])
        self.assertEqual(len(scoped["wave"]), 1)
        state = auto.load_state(other)
        self.assertEqual(state["tasks"][scoped["wave"][0]["identity"]]["spec"]["source_key"], extra["key"])
        self.assertEqual(sum(t["status"] == "pending" for t in state["tasks"].values()), 3)

    def test_preview_and_complete_text_work_share_each_wave(self):
        books = [entry(key=f"VoiceOfML/books\0preview-{i}.pdf", source=str(i + 3) * 64) for i in range(4)]
        registry = json.loads(self.store.objects[(publication.ASSETS, auto.REGISTRY)])
        rendered = {book["key"]: book for book in books}
        for book in books:
            pending = {k: v for k, v in book.items() if not k.startswith("ocr_manifest")}
            registry["files"][book["key"]] = {**pending, "status": "rendered"}
        self.store.objects[(publication.ASSETS, auto.REGISTRY)] = publication.encode(registry)
        self.store.objects[(publication.ASSETS, auto.RENDER_REGISTRY)] = publication.encode({"version": 1, "files": rendered})
        planned = auto.wave(self.store, apply=True, now=self.now, wave_size=4, preview_first=True)
        state = auto.load_state(self.store)
        self.assertEqual(sum(bool(state["tasks"][item["identity"]]["spec"].get("preview_only")) for item in planned["wave"]), 2)

    def test_live_or_queued_builders_block_timeouts_and_all_new_admission(self):
        first = auto.wave(self.store, apply=True, now=self.now, wave_size=1)["wave"]
        before = auto.load_state(self.store)["days"]
        active = [{"run_id": 123, "tasks": first}]
        for apply in (False, True):
            report = auto.wave(self.store, apply=apply, now=self.now + auto.BUILD_TIMEOUT,
                               wave_size=2, build_runs=active)
            self.assertEqual(report["wave"], [])
            self.assertEqual(report["collected"], [])
            self.assertTrue(report["admission_blocked"])
        state = auto.load_state(self.store)
        self.assertEqual(state["days"], before)
        self.assertEqual(state["tasks"][first[0]["identity"]]["status"], "building")
        # A GitHub wave can still be finishing its jobs after its records arrive.
        state["tasks"][first[0]["identity"]]["status"] = "published"
        self.store.objects[(publication.ASSETS, auto.STATE)] = publication.encode(state)
        report = auto.wave(self.store, apply=True, now=self.now, build_runs=active)
        self.assertEqual(report["wave"], [])

    def test_actions_entrypoint_checks_queue_without_relying_on_store_class_identity(self):
        active = [{"run_id": 123, "tasks": []}]
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), \
                patch.object(auto, "active_builds", return_value=active) as inspect:
            report = auto.wave(self.store, apply=True, now=self.now)
        inspect.assert_called_once_with()
        self.assertTrue(report["admission_blocked"])
        self.assertEqual(report["wave"], [])
        self.assertEqual(report["budget"]["attempts"], 0)
        before = copy.deepcopy(self.store.objects)
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), \
                patch.object(auto, "active_builds", side_effect=RuntimeError("unreadable")):
            with self.assertRaises(RuntimeError):
                auto.wave(self.store, apply=True, now=self.now)
        self.assertEqual(self.store.objects, before)

    def test_false_timeout_compensation_preserves_budget_attempt_and_audit(self):
        planned = auto.wave(self.store, apply=True, now=self.now, wave_size=2)["wave"]
        state = auto.load_state(self.store)
        for item in planned:
            state["tasks"][item["identity"]].update(status="failed", failures=3, error_type="BuildTimeout",
                                                   retry_at=(self.now + timedelta(days=1)).isoformat())
        self.store.objects[(publication.ASSETS, auto.STATE)] = publication.encode(state)
        run = {"id": 123, "repository": {"full_name": "anftm/pipeline"},
               "path": ".github/workflows/reader-v3-build.yml", "head_branch": "main",
               "event": "workflow_dispatch", "status": "in_progress"}
        jobs = [{"id": i, "name": f"build ({item['attempt']}, {item['identity']})",
                 "status": "queued" if i == 0 else "completed"} for i, item in enumerate(planned)]
        def github(path):
            return {"jobs": jobs, "total_count": 2} if "/jobs?" in path else run
        options = {"queue": "build-timeout", "build_run_ids": [123],
                   "tasks": [{**item, "failures": 3} for item in planned]}
        with patch.object(auto, "github", side_effect=github), \
                patch.dict("os.environ", {"GITHUB_ACTOR": "operator"}):
            report = auto.retry(self.store, options, apply=True)
        restored = auto.load_state(self.store)
        self.assertEqual(restored["days"], state["days"])
        self.assertEqual([item["status"] for item in report["recovered"]], ["building", "dispatch-pending"])
        for item in planned:
            task = restored["tasks"][item["identity"]]
            self.assertEqual(task["attempts"], item["attempt"])
            self.assertEqual(task["failures"], 2)
        self.assertEqual(restored["execution_timeout_recoveries"][0]["recovered"][0]["false_timeout"]["failures"], 3)

    def test_cancelled_unstarted_dispatches_reuse_charge_and_attempt_after_drain(self):
        wave = auto.wave(self.store, apply=True, now=self.now, wave_size=3)["wave"]
        before = auto.load_state(self.store)
        cancelled = {"id": 123, "repository": {"full_name": "anftm/pipeline"},
                     "path": ".github/workflows/reader-v3-build.yml", "head_branch": "main",
                     "event": "workflow_dispatch", "status": "completed", "conclusion": "cancelled",
                     "run_attempt": 1}
        options = {"queue": "build-dispatch", "cancelled_run_ids": [123], "tasks": wave[1:]}
        def github(path):
            return {"jobs": [], "total_count": 0} if "/jobs?" in path else cancelled
        with patch.object(auto, "github", side_effect=github), \
                patch.object(auto, "active_builds", return_value=[{"run_id": 456, "tasks": wave[:1]}]), \
                patch.dict("os.environ", {"GITHUB_ACTOR": "operator"}):
            report = auto.retry(self.store, options, apply=True)
        self.assertEqual(report["recovered"], wave[1:])
        state = auto.load_state(self.store)
        self.assertEqual(state["days"], before["days"])
        self.assertTrue(all(state["tasks"][t["identity"]]["status"] == "dispatch-pending" for t in wave[1:]))
        blocked = auto.wave(self.store, apply=True, now=self.now, wave_size=3)
        self.assertEqual(blocked["wave"], [])
        # Simulate collection of the retained active task. At the budget ceiling
        # the precharged queued work must still resume using its original attempt.
        state = auto.load_state(self.store)
        state["tasks"][wave[0]["identity"]]["status"] = "published"
        state["days"][self.now.date().isoformat()] = {"attempts": auto.DAILY_ATTEMPTS, "pages": auto.DAILY_PAGES}
        self.store.objects[(publication.ASSETS, auto.STATE)] = publication.encode(state)
        resumed = auto.wave(self.store, apply=True, now=self.now, wave_size=3)
        self.assertEqual(resumed["wave"], wave[1:])
        self.assertEqual(resumed["budget"], state["days"][self.now.date().isoformat()])
        self.assertEqual(auto.load_state(self.store)["dispatch_recoveries"][0]["cancelled_run_ids"], [123])

    def test_dispatch_recovery_refuses_active_jobs_or_uninspectable_queues(self):
        first = auto.wave(self.store, apply=True, now=self.now, wave_size=1)["wave"]
        options = {"queue": "build-dispatch", "cancelled_run_ids": [123], "tasks": first}
        cancelled = {"id": 123, "repository": {"full_name": "anftm/pipeline"},
                     "path": ".github/workflows/reader-v3-build.yml", "head_branch": "main",
                     "event": "workflow_dispatch", "status": "completed", "conclusion": "cancelled",
                     "run_attempt": 1}
        def github(path):
            return {"jobs": [], "total_count": 0} if "/jobs?" in path else cancelled
        before = copy.deepcopy(self.store.objects)
        for active in ([{"run_id": 456, "tasks": first}], [{"run_id": 456, "tasks": []}]):
            with patch.object(auto, "github", side_effect=github), \
                    patch.object(auto, "active_builds", return_value=active), \
                    patch.dict("os.environ", {"GITHUB_ACTOR": "operator"}):
                with self.assertRaises(ValueError):
                    auto.retry(self.store, options, apply=True)
        self.assertEqual(self.store.objects, before)

    def test_builder_records_are_collected_promoted_and_failures_retried(self):
        fixture = fixtures.V3PublicationTests()
        fixture.setUp()
        try:
            planned = auto.wave(self.store, apply=True, now=self.now, wave_size=2)["wave"]
            state = auto.load_state(self.store)
            first, second = planned
            key = state["tasks"][first["identity"]]["spec"]["source_key"]
            staged = publication.stage(self.store, fixture.candidate(key=key), fixture.bundle, apply=True)
            self.store.allowed = False  # builders are not the serialized writer
            self.store.assert_candidate_builder = lambda protocol: None
            auto.build_task(self.store, first["identity"], first["attempt"], build=Mock(return_value=staged))
            duplicate = Mock()
            recorded = auto.build_task(self.store, first["identity"], first["attempt"], build=duplicate)
            self.assertEqual(recorded["status"], "staged")
            duplicate.assert_not_called()
            auto.build_task(self.store, second["identity"], second["attempt"], build=Mock(side_effect=OSError()))
            stale = auto.build_task(self.store, second["identity"], 99, build=Mock())
            self.assertEqual(stale["status"], "stale")
            with self.assertRaises(RuntimeError):
                auto.save_state(self.store, auto.load_state(self.store))
            self.store.allowed = True
            report = auto.wave(self.store, apply=True, now=self.now + timedelta(minutes=5), wave_size=1)
            self.assertTrue(report["changed"])
            statuses = {item["id"]: item["status"] for item in report["collected"]}
            self.assertEqual(statuses, {first["identity"]: "staged", second["identity"]: "retry"})
            self.assertEqual(report["processed"][0]["status"], "published")
            self.assertIn(key, publication.current(self.store)[1]["files"])
            self.assertEqual(len(report["wave"]), 1)  # the third book; the failed one waits for retry_at
        finally:
            fixture.temp.cleanup()

    def test_lost_builder_times_out_and_unusable_text_gets_one_native_retry(self):
        report = auto.wave(self.store, apply=True, now=self.now, wave_size=1)
        identity = report["wave"][0]["identity"]
        later = auto.wave(self.store, apply=True, now=self.now + auto.BUILD_TIMEOUT, wave_size=1)
        self.assertEqual(later["collected"], [{"id": identity, "status": "retry", "error_type": "BuildTimeout"}])
        state = auto.load_state(self.store)
        task = state["tasks"][identity]
        task.update(status="needs-review", error_type="PublicationReviewRequired")
        task.pop("review_recipe", None)
        self.store.objects[(publication.ASSETS, auto.STATE)] = publication.encode(state)
        retried = auto.wave(self.store, apply=True, now=self.now + timedelta(days=2), wave_size=3)
        self.assertIn(identity, [item["identity"] for item in retried["wave"]])
        self.store.allowed = False
        self.store.assert_candidate_builder = lambda protocol: None
        auto.build_task(self.store, identity, auto.load_state(self.store)["tasks"][identity]["attempts"],
                        build=Mock(side_effect=publication.PublicationReviewRequired()))
        self.store.allowed = True
        final = auto.wave(self.store, apply=True, now=self.now + timedelta(days=2, minutes=5), wave_size=3)
        self.assertNotIn(identity, [item["identity"] for item in final["wave"]])
        self.assertEqual(auto.load_state(self.store)["tasks"][identity]["status"], "needs-review")

    def test_superseded_duplicates_leave_needs_review(self):
        state = {"version": 1, "kind": "reader-v3-automation", "days": {}, "tasks": {
            "f" * 64: {"spec": {"source_key": "old"}, "page_count": 1, "status": "needs-review", "attempts": 1}}}
        auto.discover(state, {"version": 1, "files": {}})
        self.assertEqual(state["tasks"]["f" * 64]["status"], "superseded")

    def test_live_unusable_text_can_be_replaced_by_a_complete_candidate(self):
        """A symbol-only legacy OCR layer must not block its own complete rebuild."""
        fixture = fixtures.V3PublicationTests()
        fixture.setUp()
        try:
            key = entry()["key"]
            self.store.objects[(publication.ASSETS, auto.REGISTRY)] = publication.encode(
                {"version": 1, "files": {key: entry(key=key)}})
            empty = fixture.candidate(key=key, label="●")
            staged = publication.stage(self.store, empty, fixture.bundle, apply=True)
            first = publication.promote(self.store, staged["candidate"], None, apply=True)
            active = publication.current(self.store)[1]["files"][key]
            good = fixture.candidate(key=key, label="Complete recovered body text")
            good_staged = publication.stage(self.store, good, fixture.bundle, apply=True)
            candidate = good_staged["candidate"]
            identity = "c" * 64
            state = {"version": 1, "kind": "reader-v3-automation", "days": {}, "tasks": {
                identity: {"spec": {"source_key": key, "source_sha256": "a" * 64},
                           "page_count": 1, "status": "published", "attempts": 1,
                           "active_generation": active["reading_generation"],
                           "candidate": candidate}}}
            self.store.objects[(publication.ASSETS, auto.STATE)] = publication.encode(state)
            report = auto.wave(self.store, apply=True, now=self.now, wave_size=1)
            promoted = publication.current(self.store)[1]["files"][key]
            self.assertNotEqual(promoted["reading_generation"], first["generation"])
            task = auto.load_state(self.store)["tasks"][identity]
            self.assertEqual(task["status"], "published")
            self.assertTrue(task["text_probe"]["candidate_usable"])
            self.assertFalse(task["text_probe"]["active_usable"])
            self.assertIn(identity, [item["id"] for item in report["processed"]])
        finally:
            fixture.temp.cleanup()

    def test_text_replacement_never_overrides_accepted_or_effective_layers(self):
        """The additive repair is limited to unusable raw layers."""
        active = {"source_sha256": "a" * 64, "components": {"text": "ready"}}
        spec = {"source_key": "VoiceOfML/books\0book.pdf", "source_sha256": "a" * 64}
        for active_probe, expected in (
                ({"active_usable": False, "active_revision": "raw", "active_accepted": False}, True),
                ({"active_usable": True, "active_revision": "raw", "active_accepted": False}, False),
                ({"active_usable": False, "active_revision": "effective", "active_accepted": False}, False),
                ({"active_usable": False, "active_revision": "raw", "active_accepted": True}, False)):
            task = {"spec": spec, "text_probe": {**active_probe, "matches_active": False,
                    "identity": ["candidate.json", "a" * 64, "e" * 64],
                    "candidate_usable": True, "candidate_revision": "raw",
                    "candidate_accepted": False, "same_primary": True}}
            self.assertEqual(auto._can_replace_unusable_text(task, {**active, "reading_generation": "e" * 64}), expected)
        task = {"spec": spec, "text_probe": {"active_usable": False, "active_revision": "raw",
                "active_accepted": False, "matches_active": False, "candidate_usable": True,
                "candidate_revision": "raw", "candidate_accepted": False, "same_primary": False,
                "identity": ["candidate.json", "a" * 64, "e" * 64]}}
        self.assertFalse(auto._can_replace_unusable_text(task, {**active, "reading_generation": "e" * 64}))
        task["text_probe"]["same_primary"] = True
        task["spec"] = {**spec, "preview_only": True}
        self.assertFalse(auto._can_replace_unusable_text(task, {**active, "reading_generation": "e" * 64}))
        task["spec"] = spec
        task["text_probe"]["matches_active"] = True
        self.assertFalse(auto._can_replace_unusable_text(task, {**active, "reading_generation": "e" * 64}))
        task["text_probe"]["matches_active"] = False
        self.assertFalse(auto._can_replace_unusable_text(task, {**active, "reading_generation": "f" * 64}))

    def test_native_fallback_refuses_a_primary_without_complete_text(self):
        with patch.object(publication.pdf_ocr, "probe_pdf", return_value={"classification": "scan"}):
            with self.assertRaises(publication.PublicationReviewRequired):
                publication.native_ocr({"source_sha256": "a" * 64}, None, None, "no text")

    def test_build_workflow_limits_shared_resolver_load_outside_the_writer_lock(self):
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        build = yaml.safe_load((root / ".github/workflows/reader-v3-build.yml").read_text())
        self.assertEqual(build["concurrency"]["group"], "reader-v3-build")
        self.assertEqual(build["jobs"]["build"]["strategy"]["max-parallel"], 3)
        self.assertIn("build-task", build["jobs"]["build"]["steps"][-1]["run"])
        self.assertIn("operation=auto", build["jobs"]["promote"]["steps"][0]["run"])
        self.assertIn("!cancelled()", build["jobs"]["promote"]["if"])
        self.assertIn("needs.build.result != 'cancelled'", build["jobs"]["promote"]["if"])
        publish = yaml.safe_load((root / ".github/workflows/reader-v3-publish.yml").read_text())
        operation = next(step for step in publish["jobs"]["publish"]["steps"] if step.get("id") == "operation")
        self.assertEqual(operation["env"]["GH_TOKEN"], "${{ github.token }}")
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "anftm/pipeline", "GITHUB_RUN_ID": "1",
               "GITHUB_WORKFLOW_REF": "anftm/pipeline/.github/workflows/reader-v3-build.yml@refs/heads/main",
               "READER_V3_BUILD_PROTOCOL": publication.BUILD_PROTOCOL, "GITHUB_WORKFLOW": "Reader/桶 · Build v3 Reading Candidates"}
        store = publication.CentralHubStore.__new__(publication.CentralHubStore)
        with patch.dict("os.environ", env, clear=True):
            publication.assert_stager(store)  # builders may upload immutable candidates
            with self.assertRaises(RuntimeError):
                publication.assert_writer(store)  # but never write the catalog or state
