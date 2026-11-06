"""Exercise pending snapshot scheduler with fake timers and temporary state."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import reindex_api


class SnapshotSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.original = reindex_api.snapshot_publish_timer
        reindex_api.snapshot_publish_timer = None
        self.addCleanup(setattr, reindex_api, "snapshot_publish_timer", self.original)
        self.temp = tempfile.TemporaryDirectory(prefix="bha-scheduler-", dir=str(Path(__file__).resolve().parents[2]))
        self.addCleanup(self.temp.cleanup)
        marker = patch.object(reindex_api, "publish_marker_path", return_value=Path(self.temp.name) / "marker")
        marker.start()
        self.addCleanup(marker.stop)

    def test_schedule_is_bounded_and_does_not_duplicate_live_timer(self):
        timer = MagicMock()
        timer.is_alive.return_value = True
        with patch.object(reindex_api.threading, "Timer", return_value=timer) as factory:
            reindex_api.schedule_bucket_snapshot_publish(0)
            reindex_api.schedule_bucket_snapshot_publish(60)
        factory.assert_called_once_with(1, reindex_api.publish_bucket_snapshot_worker)
        timer.start.assert_called_once()
        self.assertTrue(timer.daemon)

    def test_resume_pending_uses_retry_delay_and_invalid_timestamp(self):
        with patch.object(reindex_api, "BUCKET_DIR", Path("/fixture")), \
                patch.object(reindex_api, "bucket_snapshot_status", side_effect=[
                    {"state": "ready"}, {"state": "pending_publish", "retry_at": 110},
                    {"state": "pending_publish", "retry_at": "bad"}]), \
                patch.object(reindex_api.time, "time", return_value=100), \
                patch.object(reindex_api, "schedule_bucket_snapshot_publish") as schedule:
            reindex_api.resume_pending_snapshot_publish()
            reindex_api.resume_pending_snapshot_publish()
            reindex_api.resume_pending_snapshot_publish()
        self.assertEqual([call.args[0] for call in schedule.call_args_list], [10, 0])

    def test_worker_publishes_only_when_due_and_stops_on_failure(self):
        bucket = Path("/fixture")
        with patch.object(reindex_api, "BUCKET_DIR", bucket), \
                patch.object(reindex_api, "bucket_snapshot_status", return_value=None), \
                patch.object(reindex_api, "bucket_snapshot_publish_due", side_effect=[True, False, True]), \
                patch.object(reindex_api, "bucket_snapshot_publish_delay", return_value=23), \
                patch.object(reindex_api, "publish_bucket_snapshot", side_effect=[None, RuntimeError("offline")]) as publish, \
                patch.object(reindex_api, "write_snapshot_status") as status, \
                patch.object(reindex_api, "schedule_bucket_snapshot_publish") as schedule, \
                patch.object(reindex_api.time, "time", return_value=100):
            reindex_api.publish_bucket_snapshot_worker()
            reindex_api.publish_bucket_snapshot_worker()
            reindex_api.publish_bucket_snapshot_worker()
        self.assertEqual(publish.call_count, 2)
        self.assertEqual([call.args[0] for call in schedule.call_args_list], [23])
        pending = next(call for call in status.call_args_list if call.args[0] == "pending_publish")
        failed = next(call for call in status.call_args_list if call.args[0] == "publish_failed")
        self.assertEqual(pending.kwargs["retry_at"], 123)
        self.assertIn("offline", failed.kwargs["detail"])

    def test_actual_publication_failure_stops_automatic_retry(self):
        with patch.object(reindex_api, "BUCKET_DIR", Path("/fixture")), \
                patch.object(reindex_api, "bucket_snapshot_status", side_effect=[None, {"state": "publish_failed"}]), \
                patch.object(reindex_api, "bucket_snapshot_publish_due", return_value=True), \
                patch.object(reindex_api, "publish_bucket_snapshot", side_effect=RuntimeError("failed")), \
                patch.object(reindex_api, "write_snapshot_status") as status, \
                patch.object(reindex_api, "schedule_bucket_snapshot_publish") as schedule:
            reindex_api.publish_bucket_snapshot_worker()
        status.assert_not_called()
        schedule.assert_not_called()

    def test_competing_publication_does_not_overwrite_status_or_retry(self):
        marker = reindex_api.publish_marker_path()
        def blocked(_bucket):
            marker.write_text("other local publisher", encoding="utf-8")
            raise reindex_api.PublicationIncomplete("synthetic")
        with patch.object(reindex_api, "BUCKET_DIR", Path("/fixture")), \
                patch.object(reindex_api, "bucket_snapshot_status", return_value=None), \
                patch.object(reindex_api, "bucket_snapshot_publish_due", return_value=True), \
                patch.object(reindex_api, "publish_bucket_snapshot", side_effect=blocked), \
                patch.object(reindex_api, "write_snapshot_status") as status, \
                patch.object(reindex_api, "schedule_bucket_snapshot_publish") as schedule:
            reindex_api.publish_bucket_snapshot_worker()
        status.assert_not_called()
        schedule.assert_not_called()

    def test_previous_failed_publication_stops_before_due_check(self):
        for state in ("publish_failed", "publishing"):
            with self.subTest(state=state), patch.object(reindex_api, "BUCKET_DIR", Path("/fixture")), \
                    patch.object(reindex_api, "bucket_snapshot_status", return_value={"state": state}), \
                    patch.object(reindex_api, "bucket_snapshot_publish_due") as due, \
                    patch.object(reindex_api, "publish_bucket_snapshot") as publish:
                reindex_api.publish_bucket_snapshot_worker()
            due.assert_not_called()
            publish.assert_not_called()

    def test_interrupted_publication_is_not_automatically_retried_on_restart(self):
        state = {}
        marker = reindex_api.publish_marker_path()
        def write(value, **kwargs):
            state.clear()
            state.update(state=value, **kwargs)
        def interrupted(_bucket):
            marker.write_text("synthetic", encoding="utf-8")
            state["state"] = "publishing"
            raise SystemExit("synthetic crash")
        with patch.object(reindex_api, "BUCKET_DIR", Path("/fixture")), \
                patch.object(reindex_api, "bucket_snapshot_status", side_effect=lambda: dict(state)), \
                patch.object(reindex_api, "write_snapshot_status", side_effect=write), \
                patch.object(reindex_api, "bucket_snapshot_publish_due", return_value=True), \
                patch.object(reindex_api, "publish_bucket_snapshot", side_effect=interrupted) as publish, \
                patch.object(reindex_api, "schedule_bucket_snapshot_publish") as schedule:
            with self.assertRaises(SystemExit):
                reindex_api.publish_bucket_snapshot_worker()
            self.assertEqual(state["state"], "publishing")
            reindex_api.resume_pending_snapshot_publish()
            reindex_api.publish_bucket_snapshot_worker()
        publish.assert_called_once()
        schedule.assert_not_called()

    def test_incomplete_marker_blocks_pending_retry_even_after_status_restored(self):
        reindex_api.publish_marker_path().write_text("synthetic", encoding="utf-8")
        with patch.object(reindex_api, "BUCKET_DIR", Path("/fixture")), \
                patch.object(reindex_api, "bucket_snapshot_status", return_value={"state": "pending_publish"}), \
                patch.object(reindex_api, "schedule_bucket_snapshot_publish") as schedule, \
                patch.object(reindex_api, "publish_bucket_snapshot") as publish:
            reindex_api.resume_pending_snapshot_publish()
            reindex_api.publish_bucket_snapshot_worker()
        schedule.assert_not_called()
        publish.assert_not_called()

    def test_two_local_workers_serialize_and_second_rechecks_after_first(self):
        entered = threading.Event()
        release = threading.Event()
        observed = []
        def publish(_bucket):
            observed.append("first-start")
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release publisher")
            observed.append("first-end")
        with patch.object(reindex_api, "BUCKET_DIR", Path("/fixture")), \
                patch.object(reindex_api, "bucket_snapshot_status", return_value=None), \
                patch.object(reindex_api, "write_snapshot_status"), \
                patch.object(reindex_api, "bucket_snapshot_publish_due", side_effect=[True, False]) as due, \
                patch.object(reindex_api, "bucket_snapshot_publish_delay", return_value=23), \
                patch.object(reindex_api, "publish_bucket_snapshot", side_effect=publish), \
                patch.object(reindex_api, "schedule_bucket_snapshot_publish") as schedule:
            first = threading.Thread(target=reindex_api.publish_bucket_snapshot_worker)
            second = threading.Thread(target=reindex_api.publish_bucket_snapshot_worker)
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            release.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(observed, ["first-start", "first-end"])
        self.assertEqual(due.call_count, 2)
        schedule.assert_called_once_with(23)

    def test_job_file_persists_prior_fields_only_for_same_job(self):
        with tempfile.TemporaryDirectory(prefix="bha-jobs-") as directory, \
                patch.object(reindex_api, "REINDEX_JOB_PATH", Path(directory) / "job.json"):
            reindex_api.write_reindex_job("accepted", "one", mode="full")
            reindex_api.write_reindex_job("running", "one", started_at=123)
            self.assertEqual(reindex_api.read_reindex_job()["mode"], "full")
            reindex_api.write_reindex_job("accepted", "two")
            self.assertNotIn("mode", reindex_api.read_reindex_job())

    def test_old_worker_cannot_overwrite_newer_job_record(self):
        with tempfile.TemporaryDirectory(prefix="bha-jobs-") as directory, \
                patch.object(reindex_api, "REINDEX_JOB_PATH", Path(directory) / "job.json"):
            reindex_api.write_reindex_job("accepted", "new", mode="incremental")
            reindex_api.update_reindex_job("succeeded", "old", finished_at=1)
            self.assertEqual(reindex_api.read_reindex_job()["job_id"], "new")
            self.assertEqual(reindex_api.read_reindex_job()["state"], "accepted")

    def test_worker_rejects_changed_job_without_touching_index_or_serving_status(self):
        lock = MagicMock()
        with patch.object(reindex_api, "read_reindex_job", return_value={"job_id": "new"}), \
                patch.object(reindex_api, "ensure_index") as index, \
                patch.object(reindex_api, "update_reindex_job") as update, \
                patch.object(reindex_api.runtime, "clear_response_caches") as clear:
            reindex_api.run_reindex(lock, job_id="old")
        lock.close.assert_called_once_with()
        index.assert_not_called()
        update.assert_not_called()
        clear.assert_not_called()

    def test_job_file_changed_between_comparison_and_write_is_not_fenced(self):
        # Characterize an unresolved race: reading and then replacing a local
        # receipt is not CAS. Do not interpret the earlier sequential mismatch
        # check as protection from another host or an uncoordinated writer.
        with tempfile.TemporaryDirectory(prefix="bha-jobs-") as directory, \
                patch.object(reindex_api, "REINDEX_JOB_PATH", Path(directory) / "job.json"):
            reindex_api.write_reindex_job("accepted", "old", mode="incremental")
            real_write = reindex_api.write_reindex_job
            def foreign_write_then_old(state, job_id, **values):
                real_write("accepted", "new", mode="incremental")
                real_write(state, job_id, **values)
            with patch.object(reindex_api, "write_reindex_job", side_effect=foreign_write_then_old):
                self.assertTrue(reindex_api.update_reindex_job("succeeded", "old"))
            self.assertEqual(reindex_api.read_reindex_job()["job_id"], "old")

    def test_running_receipt_write_failure_prevents_index_work(self):
        lock = MagicMock()
        with tempfile.TemporaryDirectory(prefix="bha-jobs-") as directory, \
                patch.object(reindex_api, "read_reindex_job", return_value={"job_id": "synthetic"}), \
                patch.object(reindex_api, "update_reindex_job", return_value=False), \
                patch.object(reindex_api, "ensure_index") as index, \
                patch.object(reindex_api.runtime, "INDEX_STATUS_PATH", Path(directory) / "status"), \
                patch.object(reindex_api.runtime, "INDEX_SWITCH_STATE_PATH", Path(directory) / "switch"), \
                patch.object(reindex_api.runtime, "serving_generation_valid", return_value=True), \
                patch.object(reindex_api.runtime, "clear_response_caches"):
            reindex_api.run_reindex(lock, job_id="synthetic")
        index.assert_not_called()
        lock.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
