import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from scripts import reader_producer_audit_runner as runner


class AuditRunnerTests(unittest.TestCase):
    def context(self):
        return {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
                'GITHUB_WORKFLOW_REF': runner.WORKFLOW_REF, 'GITHUB_RUN_ID': '123'}

    def test_checked_in_inputs_are_exact_and_non_authorizing(self):
        candidate, raws, writer = runner.load_inputs()
        self.assertEqual(len(raws), 9)
        self.assertEqual(candidate['phase'], 'prepared')
        self.assertIsNone(candidate['source_writer_proofs'])
        self.assertFalse(json.loads(writer)['gc_authorized'])

    def test_invalid_context_is_rejected_before_any_store_access(self):
        store = Mock()
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(RuntimeError, 'locked central workflow'):
            runner.run(store)
        store.read.assert_not_called()
        store.create.assert_not_called()

    def test_changed_or_missing_inputs_fail_before_any_write(self):
        store = Mock()
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', self.context(), clear=True):
            with self.assertRaises(ValueError):
                runner.run(store, Path(root))
        store.read.assert_not_called()
        store.create.assert_not_called()

    def test_exact_inputs_use_only_predefined_conditional_store(self):
        remote = {}
        def read(path):
            if path not in remote:
                raise FileNotFoundError(path)
            return remote[path]
        def create(path, raw):
            if path in remote:
                raise AssertionError('must not overwrite')
            remote[path] = raw
        store = Mock(read=read, create=create)
        with patch.dict('os.environ', self.context(), clear=True):
            result = runner.run(store)
        self.assertEqual(result['object_count'], 12)
        self.assertEqual(result['candidate_sha256'], runner.CANDIDATE_SHA)
        self.assertEqual(result['intent_sha256'], runner.INTENT_SHA)
        self.assertFalse(result['scope_activated'])
        self.assertFalse(result['gc_authorized'])

    def test_old_one_time_audit_has_no_actions_entrypoint(self):
        self.assertFalse((runner.ROOT / '.github/workflows/producer-migration-audit.yml').exists())
        self.assertFalse((runner.ROOT / '.github/workflows/producer-v2-credential-preflight.yml').exists())


if __name__ == '__main__':
    unittest.main()
