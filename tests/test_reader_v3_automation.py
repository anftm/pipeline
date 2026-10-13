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
        # In-flight builds are not charged or dispatched again.
        again = auto.wave(self.store, apply=True, now=self.now, wave_size=8)
        self.assertEqual(len(again["wave"]), 1)
        self.assertEqual(again["budget"]["attempts"], 3)

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
        self.assertEqual(build["jobs"]["build"]["strategy"]["max-parallel"], 1)
        self.assertIn("build-task", build["jobs"]["build"]["steps"][-1]["run"])
        self.assertIn("operation=auto", build["jobs"]["promote"]["steps"][0]["run"])
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "anftm/pipeline", "GITHUB_RUN_ID": "1",
               "GITHUB_WORKFLOW_REF": "anftm/pipeline/.github/workflows/reader-v3-build.yml@refs/heads/main",
               "READER_V3_BUILD_PROTOCOL": publication.BUILD_PROTOCOL, "GITHUB_WORKFLOW": "Build v3 Reading Candidates"}
        store = publication.CentralHubStore.__new__(publication.CentralHubStore)
        with patch.dict("os.environ", env, clear=True):
            publication.assert_stager(store)  # builders may upload immutable candidates
            with self.assertRaises(RuntimeError):
                publication.assert_writer(store)  # but never write the catalog or state
