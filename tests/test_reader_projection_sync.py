"""Offline publication regression: unchanged OCR cannot pin stale text routing."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml

from scripts import publish_search_reader_index as publication


def condition(expression, *, operation='auto', changed='false', apply='true', paused='false',
              event='workflow_dispatch', repository='anftm/pipeline', build_result='success', cancelled=False):
    """Evaluate only our checked-in fixed Actions conditions, with no external data."""
    replacements = {
        'steps.operation.outputs.operation': operation, 'steps.operation.outputs.changed': changed,
        'steps.operation.outputs.apply': apply, 'vars.READER_V3_AUTO_PAUSED': paused,
        'inputs.operation': operation, 'github.event_name': event, 'github.repository': repository,
        'needs.build.result': build_result,
    }
    for key in sorted(replacements, key=len, reverse=True):
        expression = expression.replace(key, repr(replacements[key]))
    expression = expression.replace('!cancelled()', repr(not cancelled))
    expression = expression.replace('&&', ' and ').replace('||', ' or ').replace('always()', 'True')
    return bool(eval('(' + expression + ')', {'__builtins__': {}, 'fromJSON': json.loads, 'contains': lambda items, value: value in items}))


class ReaderProjectionSyncTests(unittest.TestCase):
    def workflow(self, name):
        return yaml.safe_load((Path(__file__).parents[1] / '.github/workflows' / name).read_text())

    def test_protocol_not_display_name_requests_base_refresh_after_full_writer_validation(self):
        events = []
        store = Mock(spec=['assert_serialized_writer'])
        store.assert_serialized_writer.side_effect = lambda protocol: events.append(('validate', protocol))
        with patch.dict(os.environ, {'READER_V3_WRITE_PROTOCOL': publication.PROTOCOL, 'GITHUB_WORKFLOW': 'renamed display label'}), \
                patch.object(publication, 'CentralHubStore', return_value=store), \
                patch.object(publication, 'unify', side_effect=lambda *args, **kwargs: events.append(('unify', kwargs['apply']))):
            self.assertTrue(publication.synchronize_pinned_base(dry_run=False))
        self.assertEqual(events, [('validate', publication.PROTOCOL), ('unify', True)])

    def test_noncentral_or_dry_run_cannot_promote_a_pointer(self):
        for protocol, dry_run in (('', False), (publication.PROTOCOL, True)):
            with self.subTest(protocol=protocol, dry_run=dry_run), \
                    patch.dict(os.environ, {'READER_V3_WRITE_PROTOCOL': protocol}), \
                    patch.object(publication, 'CentralHubStore') as store, patch.object(publication, 'unify') as unify:
                self.assertFalse(publication.synchronize_pinned_base(dry_run=dry_run))
                store.assert_not_called(); unify.assert_not_called()

    def test_forged_protocol_without_actual_workflow_ownership_fails_before_mutation(self):
        store = Mock(spec=['assert_serialized_writer']); store.assert_serialized_writer.side_effect = RuntimeError('not the real writer')
        with patch.dict(os.environ, {'READER_V3_WRITE_PROTOCOL': publication.PROTOCOL}), \
                patch.object(publication, 'CentralHubStore', return_value=store), patch.object(publication, 'unify') as unify:
            with self.assertRaises(RuntimeError): publication.synchronize_pinned_base(dry_run=False)
            unify.assert_not_called()

    def test_no_new_ocr_still_publishes_and_manual_unify_remains_available(self):
        workflow = self.workflow('reader-v3-publish.yml')
        step = next(item for item in workflow['jobs']['publish']['steps'] if item.get('name') == 'Publish the pinned projection to Pages')
        self.assertTrue(condition(step['if'], operation='auto', changed='false'))
        self.assertTrue(condition(step['if'], operation='unify', changed='true'))
        self.assertFalse(condition(step['if'], operation='auto', changed='false', apply='false'))
        self.assertFalse(condition(step['if'], operation='retire'))
        self.assertEqual(workflow['concurrency']['group'], 'reader-sidecar')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertEqual(workflow['concurrency']['queue'], 'max')

    def test_expensive_retirement_is_manual_and_small_unify_has_no_full_progress_scan(self):
        workflow = self.workflow('reader-v3-publish.yml')
        self.assertIn('retire', workflow[True]['workflow_dispatch']['inputs']['operation']['options'])
        steps = workflow['jobs']['publish']['steps']
        self.assertFalse(any('publish_reader_v3.py retire --apply' in step.get('run', '') for step in steps))
        progress = next(item for item in steps if item.get('name') == 'Write deduplicated PDF/OCR progress report')
        self.assertFalse(condition(progress['if'], operation='unify'))
        self.assertTrue(condition(progress['if'], operation='auto'))

    def test_pause_blocks_only_auto_admission_and_build_callbacks_not_manual_sync(self):
        workflow = self.workflow('reader-v3-publish.yml')
        gate = workflow['jobs']['publish']['if']
        self.assertFalse(condition(gate, operation='auto', paused='true'))
        self.assertFalse(condition(gate, paused='true', event='schedule'))
        self.assertTrue(condition(gate, operation='unify', paused='true'))
        self.assertTrue(condition(gate, operation='auto', paused='false'))
        self.assertFalse(condition(gate, operation='unify', paused='true', repository='other/repo'))
        build = self.workflow('reader-v3-build.yml')
        for job in build['jobs'].values():
            self.assertFalse(condition(job['if'], paused='true'))
            self.assertTrue(condition(job['if'], paused='false'))
        callback = build['jobs']['promote']['if']
        self.assertFalse(condition(callback, cancelled=True))
        self.assertFalse(condition(callback, build_result='cancelled'))
        self.assertFalse(condition(callback, build_result='skipped'))
        self.assertTrue(condition(callback, build_result='failure'))


if __name__ == '__main__':
    unittest.main()
