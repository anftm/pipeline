"""A dispatched workflow is only a correlated run, not a site receipt."""
import unittest
from unittest.mock import Mock

from scripts import search_pages_deploy as pages

OP = "a" * 32
SHA = "b" * 40
GEN = "c" * 64
TIME = "2026-01-01T00:00:00Z"


def run(**changes):
    return {"id": 100, "display_title": pages.run_name(OP), "created_at": TIME,
            "event": "workflow_dispatch", "head_branch": "main", "head_sha": SHA,
            "path": ".github/workflows/static.yml@refs/heads/main", "status": "in_progress",
            "conclusion": None, **changes}


class PagesDeployTests(unittest.TestCase):
    def test_exactly_one_matching_run_and_no_false_receipt(self):
        value = run()
        self.assertEqual(pages.matching_run([value], operation=OP, not_before=TIME, expected_sha=SHA), value)
        self.assertIsNone(pages.matching_run([], operation=OP, not_before=TIME, expected_sha=SHA))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            pages.matching_run([value, value], operation=OP, not_before=TIME, expected_sha=SHA)

    def test_mismatch_drift_and_unknown_status_are_rejected(self):
        variants = ({"head_sha": "d" * 40}, {"event": "push"}, {"head_branch": "feature"},
                    {"path": None}, {"status": "surprise"}, {"id": True},
                    {"status": "completed", "conclusion": None}, {"created_at": "2025-01-01T00:00:00Z"})
        for patch in variants:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                pages.matching_run([run(**patch)], operation=OP, not_before=TIME, expected_sha=SHA)

    def test_dispatch_is_one_attempt_even_for_unknown_outcome(self):
        session = Mock()
        session.post.return_value.status_code = 204
        pages.dispatch(session, generation=GEN, operation=OP)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.post.call_args.kwargs["json"]["inputs"],
                         {"search_generation": GEN, "search_transition": OP})
        session.reset_mock()
        session.post.side_effect = TimeoutError("lost")
        with self.assertRaises(TimeoutError):
            pages.dispatch(session, generation=GEN, operation=OP)
        self.assertEqual(session.post.call_count, 1)
        session.post.side_effect = None
        session.post.return_value.status_code = 500
        with self.assertRaisesRegex(RuntimeError, "inspect before any retry"):
            pages.dispatch(session, generation=GEN, operation=OP)

    def test_bounded_run_lookup_rejects_pagination(self):
        response = Mock()
        response.links = {}
        response.json.return_value = {"workflow_runs": [run()]}
        session = Mock()
        session.get.return_value = response
        self.assertEqual(pages.inspect_runs(session, operation=OP, not_before=TIME, expected_sha=SHA)["id"], 100)
        response.links = {"next": {"url": "other"}}
        with self.assertRaisesRegex(ValueError, "paginated"):
            pages.inspect_runs(session, operation=OP, not_before=TIME, expected_sha=SHA)
        response.links = {}
        response.json.return_value = {"workflow_runs": None}
        with self.assertRaisesRegex(TypeError, "invalid Pages"):
            pages.inspect_runs(session, operation=OP, not_before=TIME, expected_sha=SHA)

    def test_inputs_cannot_change_repo_or_inject_operation(self):
        session = Mock()
        for generation, operation in (("bad", OP), (GEN, "a" * 31), (GEN, "A" * 32)):
            with self.subTest(generation=generation, operation=operation), self.assertRaises(ValueError):
                pages.dispatch(session, generation=generation, operation=operation)
        session.post.assert_not_called()
        with self.assertRaises(ValueError):
            pages.matching_run([], operation=OP, not_before="yesterday", expected_sha=SHA)
        with self.assertRaises(ValueError):
            pages.matching_run([], operation=OP, not_before=TIME, expected_sha="main")


if __name__ == "__main__":
    unittest.main()
