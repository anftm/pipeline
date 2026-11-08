"""Space variable writes are single-shot and do not claim writer fencing."""
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import search_space_deploy as space

A, B, SHA = "a" * 64, "b" * 64, "c" * 40
OLD, NEW = "2026-11-08T00:00:00Z", "2026-11-08T00:01:00Z"


def variable(value, updated=OLD):
    return {space.VARIABLE: SimpleNamespace(value=value, updated_at=updated)}


def api_mock():
    api = Mock()
    api.space_info.return_value = SimpleNamespace(sha=SHA)
    api.get_space_runtime.return_value = SimpleNamespace(stage="RUNNING", raw={"sha": SHA})
    api.get_space_variables.return_value = variable(A)
    return api


class SpaceDeployTests(unittest.TestCase):
    def test_successful_single_write_and_readback(self):
        api = api_mock()
        api.get_space_variables.side_effect = [variable(A), variable(B, NEW)]
        space.set_generation(api, expected=A, expected_updated_at=OLD, target=B, expected_sha=SHA)
        api.add_space_variable.assert_called_once_with(space.SPACE, key=space.VARIABLE, value=B)

    def test_lost_write_response_never_replays(self):
        api = api_mock()
        api.add_space_variable.side_effect = TimeoutError("unknown outcome")
        with self.assertRaises(TimeoutError):
            space.set_generation(api, expected=A, expected_updated_at=OLD, target=B, expected_sha=SHA)
        self.assertEqual(api.add_space_variable.call_count, 1)
        self.assertEqual(api.get_space_variables.call_count, 1)

    def test_changed_variable_or_revision_stops_before_write(self):
        for effect in ("revision", "runtime", "stage", "value", "timestamp"):
            api = api_mock()
            if effect == "revision":
                api.space_info.return_value.sha = "d" * 40
            elif effect == "runtime":
                api.get_space_runtime.return_value.raw["sha"] = "d" * 40
            elif effect == "stage":
                api.get_space_runtime.return_value.stage = "RUNNING_BUILDING"
            elif effect == "value":
                api.get_space_variables.return_value = variable(B)
            else:
                api.get_space_variables.return_value = variable(A, NEW)
            with self.subTest(effect=effect), self.assertRaises(ValueError):
                space.set_generation(api, expected=A, expected_updated_at=OLD, target=B, expected_sha=SHA)
            api.add_space_variable.assert_not_called()

    def test_wrong_readback_stops_and_does_not_replay(self):
        api = api_mock()
        api.get_space_variables.side_effect = [variable(A), variable(A, NEW)]
        with self.assertRaisesRegex(RuntimeError, "inspect before recovery"):
            space.set_generation(api, expected=A, expected_updated_at=OLD, target=B, expected_sha=SHA)
        api.add_space_variable.assert_called_once()

    def test_running_revision_and_variable_are_both_required_for_receipt(self):
        api = api_mock()
        api.get_space_variables.return_value = variable(B, NEW)
        self.assertIsInstance(space.inspect(api, expected=B, expected_sha=SHA, expected_updated_after=OLD), bytes)
        with self.assertRaisesRegex(ValueError, "expected update"):
            space.inspect(api, expected=B, expected_sha=SHA, expected_updated_after="2026-11-09T00:00:00Z")
        api.get_space_runtime.return_value.stage = "BUILDING"
        with self.assertRaisesRegex(ValueError, "not running"):
            space.inspect(api, expected=B, expected_sha=SHA, expected_updated_after=OLD)

    def test_timestamps_and_missing_variable_fail_closed(self):
        api = api_mock()
        api.get_space_variables.return_value = variable(A, datetime(2026, 11, 8, tzinfo=UTC))
        self.assertEqual(space.variable(api), (A, OLD))
        for value in (None, "not-time", "2026-11-08T00:00:00", "2026-11-08T00:00:00+01:00"):
            api.get_space_variables.return_value = variable(A, value)
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                space.variable(api)
        api.get_space_variables.return_value = {}
        with self.assertRaises(ValueError):
            space.variable(api)
        with self.assertRaises(ValueError):
            space.set_generation(api, expected=A, expected_updated_at=OLD, target=A, expected_sha=SHA)
        api.add_space_variable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
