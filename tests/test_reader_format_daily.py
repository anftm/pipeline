"""Daily dispatch safety and completion, with no remote mutations."""
from contextlib import nullcontext
import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import requests

from scripts import reader_format_daily as subject

HEAD = 'a' * 40
SEARCH, READER, NEXT = '1' * 64, '2' * 64, '3' * 64


def summary(count=10, extension='epub', **changes):
    formats = []
    for ext, (family, workflow, _limit) in subject.FORMATS.items():
        row = dict.fromkeys(subject.COUNTS, 0)
        row.update(extension=ext, family=family, workflow=workflow,
                   all_source_identities_checked=True, conversion_dependencies_checked=True)
        if ext == extension:
            row.update(sources=30, not_mapped=count, definite_backlog=count, source_content_unchanged=30-count)
            row.update(changes)
        formats.append(row)
    return {'version': 1, 'kind': 'reader-format-backlog', 'threshold': 10,
            'search_generation': SEARCH, 'reader_generation': READER, 'formats': formats}


def receipt(generation=READER):
    return {'complete': True, 'generation': generation,
            'consumers': {surface: {'generation': generation, 'projection_verified': True}
                          for surface in ('hf', 'pages')}}


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class FakeAPI:
    def __init__(self, journal):
        self.journal, self.posts, self.runs = journal, [], []
        self.unknown, self.invisible, self.active = False, False, []
        self.main = HEAD
        self.mutate = lambda run: None
        self.head_checks = 0
        self.artifact_generation = READER

    def check_main(self, expected):
        self.head_checks += 1
        if self.main != expected:
            raise RuntimeError('pipeline main differs from expected HEAD')

    def active_children(self, parent_run_id=None): return self.active

    def dispatch(self, workflow, inputs):
        saved = json.loads(self.journal.read_text())
        assert saved['operations'][-1]['status'] == 'dispatch_intent'
        assert saved['operations'][-1]['inputs'] == inputs
        self.posts.append((workflow, inputs))
        run = {'id': len(self.posts), 'head_sha': HEAD, 'head_branch': 'main', 'event': 'workflow_dispatch',
               'run_attempt': 1, 'path': '.github/workflows/' + workflow,
               'display_title': 'reader-auto/' + inputs['automation_id'],
               'status': 'completed', 'conclusion': 'success'}
        self.mutate(run)
        self.runs.append(run)
        if self.unknown:
            raise TimeoutError('response lost after accepted dispatch')

    def find_operation(self, operation):
        if self.invisible: return []
        return [run for run in self.runs if run['display_title'] == operation['run_name']]

    def get_run(self, run_id): return next(run for run in self.runs if run['id'] == run_id)

    def publication_report(self, operation, expected_sha):
        assert operation['workflow'] == subject.PUBLISH
        assert expected_sha == HEAD
        return {'generation': self.artifact_generation, 'verification': receipt(self.artifact_generation),
                'artifact_id': 99, 'run_id': operation['run_id'], 'run_attempt': 1}


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'journal.json'
        self.api, self.clock = FakeAPI(self.journal), Clock()
        self.refresh = Mock(side_effect=[summary(10), summary(0)])
        self.verify = Mock(return_value=receipt())

    def controller(self, **kwargs):
        options = dict(api=self.api, refresh=self.refresh, verify=self.verify, journal=self.journal,
                       expected_sha=HEAD, enabled=True, known_writers=True,
                       clock=self.clock, sleep=self.clock.sleep, timeout_seconds=60)
        options.update(kwargs)
        return subject.Controller(**options)

    def test_threshold_for_all_supported_formats_and_exclusions(self):
        for ext, (_family, workflow, _limit) in subject.FORMATS.items():
            for count in (0, 9, 10, 11):
                with self.subTest(ext=ext, count=count):
                    result = subject.plan(summary(count, ext))
                    expected = ('blocked' if workflow is None else 'triggered' if count >= 10
                                else 'complete' if count == 0 else 'below_threshold')
                    self.assertEqual(result['formats'][ext]['state'], expected)
                    self.assertEqual(result['operation_ids'], [])
        data = summary()
        data['formats'].extend([{'extension': 'pdf'}, {'extension': 'djvu'}])
        result = subject.plan(data)
        self.assertEqual(result['formats']['pdf']['state'], 'excluded')
        self.assertEqual(result['formats']['djvu']['state'], 'excluded')

    def test_flags_unknown_pending_failure_block_even_with_zero_backlog(self):
        for changes in ({'all_source_identities_checked': False}, {'conversion_dependencies_checked': False},
                        {'pending_shard_checkpoint': 1}, {'recorded_failure_current': 1},
                        {'source_identity_unknown': 1, 'source_content_unchanged': 29},
                        {'mapped_content_not_checked': 1, 'source_content_unchanged': 29}):
            with self.subTest(changes=changes):
                result = subject.plan(summary(0, **changes))
                self.assertEqual(result['formats']['epub']['state'], 'blocked')
                self.assertIn('epub', result['blocked'])

    def test_new_web_sources_trigger_without_claiming_dependencies_checked(self):
        for ext in subject.FAMILIES['web'][0].split():
            for count in (9, 10, 11):
                with self.subTest(ext=ext, count=count):
                    data = summary(count, ext, conversion_dependencies_checked=False)
                    before = copy.deepcopy(data)
                    decision = subject.plan(data)['formats'][ext]
                    self.assertEqual(decision['state'], 'triggered' if count >= 10 else 'below_threshold')
                    self.assertEqual(decision['completion_scope'], 'explicit_source_changes')
                    self.assertFalse(decision['conversion_dependencies_checked'])
                    self.assertEqual(decision['dependency_freshness'], 'unknown')
                    self.assertTrue(decision['warnings'])
                    self.assertEqual(data, before)

    def test_source_change_exception_never_hides_unknown_pending_or_failure(self):
        changes = ({'source_identity_unknown': 1, 'source_content_unchanged': 19},
                   {'mapped_content_not_checked': 1, 'source_content_unchanged': 19},
                   {'pending_shard_checkpoint': 1}, {'recorded_failure_current': 1},
                   {'all_source_identities_checked': False})
        for extra in changes:
            with self.subTest(extra=extra):
                data = summary(10, 'html', conversion_dependencies_checked=False, **extra)
                decision = subject.plan(data)['formats']['html']
                self.assertEqual(decision['state'], 'blocked')
                self.assertIn('conversion_dependencies_checked is not true', decision['reasons'])
        for ext in subject.FAMILIES['text'][0].split():
            dependent = ext in {'md', 'markdown'}
            decision = subject.plan(summary(10, ext, conversion_dependencies_checked=not dependent))['formats'][ext]
            self.assertEqual(decision['state'], 'triggered')
            self.assertEqual(decision['workflow'], 'reader-text-stream.yml')
            if dependent:
                self.assertEqual(decision['completion_scope'], 'explicit_source_changes')
                changed = subject.plan(summary(10, ext, not_mapped=9, source_content_changed=1,
                                               conversion_dependencies_checked=False))['formats'][ext]
                self.assertEqual(changed['state'], 'triggered')

    def test_web_first_conversion_drains_to_zero_with_unknown_dependency_freshness(self):
        data = [summary(n, 'html', conversion_dependencies_checked=False) for n in (10, 9, 0)]
        self.refresh.side_effect = data
        result = self.controller().run(data[0])
        decision = result['formats']['html']
        self.assertEqual(decision['state'], 'complete')
        self.assertEqual(decision['counts']['definite_backlog'], 0)
        self.assertEqual(decision['completion_scope'], 'explicit_source_changes')
        self.assertEqual(decision['dependency_freshness'], 'unknown')
        self.assertFalse(decision['conversion_dependencies_checked'])
        self.assertTrue(decision['warnings'])
        self.assertNotIn('html', result['blocked'])
        self.assertEqual(result['batches'], 2)
        self.assertEqual(len(self.api.posts), 4)
        self.assertEqual(self.api.posts[0][0], 'reader-web-stream.yml')
        # Zero known source backlog is complete, while dependency freshness stays unknown.
        final = subject.plan(data[-1])['formats']['html']
        self.assertEqual(final['state'], 'complete')
        self.assertEqual(final['dependency_freshness'], 'unknown')
        self.assertTrue(final['warnings'])

    def test_web_pending_or_failure_after_conversion_still_blocks(self):
        for index, changes in enumerate(({'pending_shard_checkpoint': 1}, {'recorded_failure_current': 1})):
            with self.subTest(changes=changes):
                self.journal = Path(self.temp.name) / str(index)
                self.api = FakeAPI(self.journal)
                before = summary(10, 'html', conversion_dependencies_checked=False)
                after = summary(9, 'html', conversion_dependencies_checked=False, **changes)
                self.refresh.side_effect = [before, after]
                result = self.controller().run(before)
                self.assertEqual(result['formats']['html']['state'], 'blocked')
                self.assertIn('html', result['blocked'])
                self.assertEqual(len(self.api.posts), 2)

    def test_zero_web_backlog_keeps_warning_without_blocking_known_work(self):
        data = summary(0, 'html', conversion_dependencies_checked=False)
        self.refresh.side_effect = [data]
        result = self.controller().run(data)
        self.assertEqual(result['formats']['html']['state'], 'complete')
        self.assertEqual(result['formats']['html']['dependency_freshness'], 'unknown')
        self.assertTrue(result['formats']['html']['warnings'])
        self.assertFalse(self.api.posts)

    def test_real_md_48_new_39_changed_dispatches_and_drains_known_changes(self):
        for ext in ('md', 'markdown', *subject.FAMILIES['web'][0].split()):
            with self.subTest(ext=ext):
                self.journal = Path(self.temp.name) / ext
                self.api = FakeAPI(self.journal)
                states = [summary(new + changed, ext, sources=100, not_mapped=new,
                                  source_content_changed=changed, source_content_unchanged=100-new-changed,
                                  conversion_dependencies_checked=False)
                          for new, changed in ((48, 39), (0, 39), (0, 9), (0, 0))]
                self.refresh.side_effect = states
                result = self.controller().run(states[0])
                decision = result['formats'][ext]
                self.assertEqual(result['status'], 'complete')
                self.assertEqual(decision['state'], 'complete')
                self.assertEqual(decision['counts']['definite_backlog'], 0)
                self.assertEqual(decision['completion_scope'], 'explicit_source_changes')
                self.assertFalse(decision['conversion_dependencies_checked'])
                self.assertEqual(decision['dependency_freshness'], 'unknown')
                self.assertIn('Dependency-only changes to unchanged sources are not counted', decision['warnings'][0])
                self.assertIn('known source changes only', decision['warnings'][0])
                self.assertEqual(result['batches'], 3)
                for workflow, inputs in self.api.posts[::2]:
                    self.assertEqual(workflow, subject.FORMATS[ext][1])
                    self.assertEqual(inputs['extension'], ext)
                    self.assertEqual(inputs['expected_sha'], HEAD)

    def test_mixed_source_changes_threshold_and_uncertainty(self):
        for ext in ('md', 'markdown', *subject.FAMILIES['web'][0].split()):
            for count in (9, 10):
                with self.subTest(ext=ext, count=count):
                    data = summary(count, ext, not_mapped=count-1, source_content_changed=1,
                                   conversion_dependencies_checked=False)
                    self.assertEqual(subject.plan(data)['formats'][ext]['state'],
                                     'triggered' if count == 10 else 'below_threshold')
            for field in (*subject.UNCERTAIN, 'all_source_identities_checked'):
                data = summary(10, ext, not_mapped=2, source_content_changed=8,
                               conversion_dependencies_checked=False)
                row = next(row for row in data['formats'] if row['extension'] == ext)
                row[field] = False if field == 'all_source_identities_checked' else 1
                if field in ('source_identity_unknown', 'mapped_content_not_checked'):
                    row['source_content_unchanged'] -= 1
                self.assertEqual(subject.plan(data)['formats'][ext]['state'], 'blocked')

    def test_empty_dependent_formats_warn_but_do_not_block_the_round(self):
        before, after = summary(10), summary(0)
        dependent = {'md', 'markdown', *subject.FAMILIES['web'][0].split()}
        for data in (before, after):
            for row in data['formats']:
                if row['extension'] in dependent:
                    row['conversion_dependencies_checked'] = False
        self.refresh.side_effect = [before, after]
        result = self.controller().run(before)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.api.posts), 2)
        for ext in dependent:
            decision = result['formats'][ext]
            self.assertEqual(decision['state'], 'complete')
            self.assertEqual(decision['completion_scope'], 'no_sources')
            self.assertEqual(decision['dependency_freshness'], 'unknown')
            self.assertFalse(decision['conversion_dependencies_checked'])
            self.assertTrue(decision['warnings'])

    def test_changed_sources_count_toward_ten_without_trusting_trigger_ready(self):
        data = summary(10, not_mapped=2, source_content_changed=8, trigger_ready=False, threshold_exceeded=False)
        self.assertEqual(subject.plan(data)['formats']['epub']['state'], 'triggered')

    def test_untrusted_summary_rejected(self):
        for changes in ({'workflow': 'retire.yml'}, {'family': 'image'}, {'definite_backlog': 11},
                        {'not_mapped': True}, {'all_source_identities_checked': 'true'},
                        {'source_content_unchanged': 0}, {'sources': -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                subject.plan(summary(**changes))
        data = summary()
        data['formats'].append(copy.deepcopy(data['formats'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'): subject.plan(data)
        data = summary(); data['formats'][0]['extension'] = 'evil'
        with self.assertRaisesRegex(ValueError, 'unsupported'): subject.plan(data)
        data = summary(); data['formats'].pop()
        with self.assertRaisesRegex(ValueError, 'incomplete'): subject.plan(data)

    def test_inputs_match_bounded_producers(self):
        for ext, (family, workflow, limit) in subject.FORMATS.items():
            if not workflow: continue
            with self.subTest(ext=ext):
                inputs = subject.producer_inputs(ext, 'f' * 32, expected_sha=HEAD, search_generation=SEARCH)
                self.assertEqual(inputs['expected_sha'], HEAD)
                self.assertEqual(inputs['search_generation'], SEARCH)
                self.assertEqual(inputs['limit'], str(limit))
                self.assertEqual(inputs['apply'], 'true')
                self.assertEqual(inputs['defer_unify'], 'true')
                self.assertEqual(inputs['retry_failed' if family == 'chm' else 'retry_failures'], 'false')
                if family != 'chm': self.assertEqual(inputs['extension'], ext)
                if family == 'media': self.assertIn(inputs['kind'], ('audio', 'video', 'swf'))
        self.assertEqual(len({value[1] for value in subject.FORMATS.values() if value[1]}), 9)

    def test_triggered_format_drains_10_to_9_to_1_to_zero(self):
        self.refresh.side_effect = [summary(n) for n in (10, 9, 1, 0)]
        result = self.controller().run(summary())
        self.assertEqual(result['formats']['epub']['state'], 'complete')
        self.assertEqual(result['formats']['epub']['counts']['definite_backlog'], 0)
        self.assertEqual(result['batches'], 3)
        self.assertEqual(len(self.api.posts), 6)
        self.assertEqual(len(set(result['operation_ids'])), 6)
        self.assertEqual(self.verify.call_count, 3)
        self.assertEqual(self.refresh.call_count, 4)
        for producer, unify in zip(self.api.posts[::2], self.api.posts[1::2]):
            self.assertEqual(producer[0], 'reader-ebook-stream.yml')
            self.assertEqual(unify[0], subject.PUBLISH)
            self.assertEqual(set(unify[1]), {'operation', 'apply', 'automation_id'})
            self.assertEqual(unify[1]['operation'], 'unify')
        self.assertEqual(result['status'], 'complete')
        self.assertNotIn('txt', result['blocked'])

    def test_nine_initially_does_not_dispatch(self):
        self.refresh.side_effect = [summary(9)]
        result = self.controller().run(summary(9))
        self.assertEqual(result['formats']['epub']['state'], 'below_threshold')
        self.assertFalse(self.api.posts)

    def test_reader_generation_can_advance_search_must_stay_pinned(self):
        after = summary(0); after['reader_generation'] = NEXT
        self.refresh.side_effect = [summary(10), after]
        self.verify.return_value = receipt(NEXT)
        self.api.artifact_generation = NEXT
        result = self.controller().run(summary())
        self.assertEqual(result['reader_generation'], NEXT)
        self.assertEqual(result['formats']['epub']['state'], 'complete')
        self.assertNotIn('error', result)

    def test_artifact_failure_never_falls_back_to_current_pointer(self):
        self.api.publication_report = Mock(side_effect=RuntimeError('artifact mismatch'))
        result = self.controller().run(summary())
        self.assertIn('artifact mismatch', result['error'])
        self.verify.assert_not_called()
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(result['formats']['epub']['state'], 'blocked')

    def test_live_callback_uses_artifact_generation_as_expected_target(self):
        from scripts import publish_reader_v3 as publication, reader_format_backlog as backlog
        with patch.object(backlog, 'HubBucketStore'), patch('huggingface_hub.HfApi'), \
                patch('huggingface_hub.HfFileSystem'), patch('httpx.Client'), \
                patch.object(publication, 'current', side_effect=AssertionError('must not choose current')), \
                patch.object(publication, 'verify_generation', return_value=receipt(READER)) as verify:
            _refresh, callback = subject.live_callbacks()
            self.assertEqual(callback(READER, 60)['generation'], READER)
            self.assertEqual(verify.call_args.args[2], READER)
            self.assertEqual(verify.call_args.kwargs['timeout_seconds'], 60)

    def test_foreign_live_generation_cannot_replace_artifact_generation(self):
        self.verify.return_value = receipt(NEXT)
        result = self.controller().run(summary())
        self.assertIn('strict verify-generation', result['error'])
        self.assertEqual(self.verify.call_args.args[0], READER)
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(result['formats']['epub']['state'], 'blocked')

    def test_foreign_generation_after_live_verify_blocks_refresh_acceptance(self):
        after = summary(0); after['reader_generation'] = NEXT
        self.refresh.side_effect = [summary(10), after]
        result = self.controller().run(summary())
        self.assertIn('Reader generation changed', result['error'])
        self.assertEqual(result['formats']['epub']['state'], 'blocked')
        self.assertEqual(result['operations'][-1]['publication_artifact']['generation'], READER)

    def test_drift_and_zero_progress_stop(self):
        cases = []
        after = summary(9); after['search_generation'] = NEXT; cases.append((after, 'search generation'))
        after = summary(9); after['source_revisions'] = {'repo': HEAD}; cases.append((after, 'source revisions'))
        after = summary(9, sources=31, source_content_unchanged=22); cases.append((after, 'source inventory'))
        cases.append((summary(10), 'zero progress'))
        cases.append((summary(11), 'zero progress'))
        for index, (after, message) in enumerate(cases):
            with self.subTest(message=message):
                self.journal = Path(self.temp.name) / str(index)
                self.api = FakeAPI(self.journal)
                self.refresh.side_effect = [summary(10), after]
                result = self.controller().run(summary())
                self.assertIn(message, result['error'])
                self.assertEqual(len(self.api.posts), 2)
                self.assertEqual(result['formats']['epub']['state'], 'blocked')

    def test_pending_after_publish_cannot_be_complete_or_retried(self):
        self.refresh.side_effect = [summary(10), summary(0, pending_shard_checkpoint=1)]
        result = self.controller().run(summary())
        self.assertEqual(result['formats']['epub']['state'], 'blocked')
        self.assertIn('pending_shard_checkpoint', result['blocked']['epub'])
        self.assertEqual(len(self.api.posts), 2)

    def test_verification_must_confirm_both_surfaces(self):
        self.verify.return_value = {'complete': True, 'generation': READER, 'consumers': {}}
        result = self.controller().run(summary())
        self.assertIn('strict verify-generation', result['error'])
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(len(self.api.posts), 2)

    def test_unknown_dispatch_observes_without_reposting(self):
        self.api.unknown = True
        result = self.controller().run(summary())
        self.assertEqual(result['formats']['epub']['state'], 'complete')
        self.assertEqual(len(self.api.posts), 2)
        self.assertTrue(all(op['dispatch_error'] == 'TimeoutError' for op in result['operations']))

    def test_unknown_invisible_dispatch_times_out_and_observe_never_posts(self):
        self.api.unknown, self.api.invisible = True, True
        result = self.controller().run(summary())
        self.assertIn('wall timeout', result['error'])
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(result['operations'][0]['status'], 'dispatch_unknown')
        self.api.invisible = False
        result = self.controller().observe()
        self.assertEqual(result['operations'][0]['conclusion'], 'success')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)
        with self.assertRaises(FileExistsError): self.controller().run(summary())

    def test_wrong_run_identity_failure_or_duplicate_prevents_unify(self):
        for index, changes in enumerate(({'head_sha': 'b'*40}, {'head_branch': 'other'},
                                        {'event': 'push'}, {'run_attempt': 2}, {'run_attempt': True},
                                        {'path': '.github/workflows/other.yml'}, {'conclusion': 'failure'})):
            with self.subTest(changes=changes):
                self.journal = Path(self.temp.name) / str(index)
                self.api = FakeAPI(self.journal)
                self.api.mutate = lambda run: run.update(changes)
                self.refresh.side_effect = [summary(10)]
                result = self.controller().run(summary())
                self.assertIn('error', result)
                self.assertEqual(len(self.api.posts), 1)
        self.journal = Path(self.temp.name) / 'duplicate'
        self.api = FakeAPI(self.journal)
        find = self.api.find_operation
        self.api.find_operation = lambda operation: find(operation) * 2
        self.refresh.side_effect = [summary(10)]
        result = self.controller().run(summary())
        self.assertIn('multiple runs', result['error'])
        self.assertEqual(len(self.api.posts), 1)

    def test_active_child_main_drift_and_gates_prevent_first_post(self):
        for index, condition in enumerate(('active', 'main', 'enabled', 'known_writers')):
            with self.subTest(condition=condition):
                self.journal = Path(self.temp.name) / str(index)
                self.api = FakeAPI(self.journal)
                kwargs = {}
                if condition == 'active': self.api.active = [{'id': 99}]
                elif condition == 'main': self.api.main = 'b'*40
                else: kwargs[condition] = False
                result = self.controller(**kwargs).run(summary())
                self.assertIn('error', result)
                self.assertFalse(self.api.posts)

    def test_main_is_rechecked_before_each_dispatch(self):
        original = self.api.dispatch
        def dispatch(workflow, inputs):
            original(workflow, inputs)
            self.api.main = 'b'*40
        self.api.dispatch = dispatch
        result = self.controller().run(summary())
        self.assertIn('main differs', result['error'])
        self.assertEqual(len(self.api.posts), 1)

    def test_batch_budget_and_wall_bound(self):
        for kwargs in ({'max_batches': 21}, {'max_batches': 0}, {'max_batches': True},
                       {'timeout_seconds': 18001}, {'timeout_seconds': 0}, {'run_attempt': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.controller(**kwargs)
        self.refresh.side_effect = [summary(10), summary(9)]
        result = self.controller(max_batches=1, parent_run_id='100').run(summary())
        self.assertEqual(result['status'], 'continuation_required')
        self.assertEqual(result['remaining_formats'], ['epub'])
        self.assertEqual(result['formats']['epub']['counts']['definite_backlog'], 9)
        self.assertEqual(result['formats']['epub']['state'], 'continuation_required')
        self.assertEqual(result['current_summary'], summary(9))
        self.assertEqual(len(self.api.posts), 2)

    def test_refresh_over_budget_prevents_dispatch(self):
        def refresh():
            self.clock.now += 61
            return summary()
        self.refresh.side_effect = refresh
        result = self.controller().run(summary())
        self.assertIn('wall timeout', result['error'])
        self.assertFalse(self.api.posts)

    def test_wall_interrupt_during_dispatch_preserves_unknown_intent(self):
        def interrupted(_workflow, _inputs):
            raise subject.WallTimeout('wall timeout')
        self.api.dispatch = interrupted
        result = self.controller().run(summary())
        self.assertIn('WallTimeout', result['error'])
        self.assertEqual(result['operations'][0]['status'], 'dispatch_intent')
        self.assertEqual(json.loads(self.journal.read_text())['status'], 'blocked')

    def test_wall_timer_is_installed_and_restored(self):
        with patch.object(subject.signal, 'signal', return_value='previous') as handler, \
                patch.object(subject.signal, 'setitimer') as timer:
            with subject.wall_timeout(18000):
                callback = handler.call_args.args[1]
                with self.assertRaises(subject.WallTimeout): callback(None, None)
            self.assertEqual(timer.call_args_list[0].args[1], 18000)
            self.assertEqual(timer.call_args_list[-1].args[1], 0)
            self.assertEqual(handler.call_args.args[1], 'previous')

    def test_refresh_new_pending_in_other_format_is_reported(self):
        after = summary(0)
        row = next(row for row in after['formats'] if row['extension'] == 'jpg')
        row.update(sources=1, source_content_unchanged=1, pending_shard_checkpoint=1)
        before = summary(10)
        row = next(row for row in before['formats'] if row['extension'] == 'jpg')
        row.update(sources=1, source_content_unchanged=1)
        self.refresh.side_effect = [before, after]
        result = self.controller().run(before)
        self.assertIn('pending_shard_checkpoint', result['blocked']['jpg'])
        self.assertEqual(result['formats']['epub']['state'], 'complete')

    def test_plan_cli_has_no_remote_dependencies(self):
        source, output = Path(self.temp.name)/'summary.json', Path(self.temp.name)/'plan.json'
        source.write_text(json.dumps(summary()))
        with patch.object(subject, 'GitHub', side_effect=AssertionError('no network')), \
                patch.object(subject, 'live_callbacks', side_effect=AssertionError('no Hub')), patch('builtins.print'):
            self.assertEqual(subject.main(['plan', '--summary', str(source), '--output', str(output)]), 0)
        self.assertEqual(json.loads(output.read_text())['formats']['epub']['state'], 'triggered')


class ContinuationTests(unittest.TestCase):
    setUp = DailyTests.setUp
    controller = DailyTests.controller

    def prepared(self, **kwargs):
        self.refresh.side_effect = [summary(10), summary(9)]
        return self.controller(parent_run_id='100', max_batches=1, **kwargs).run(summary())

    def test_resume_below_threshold_preserves_trigger_and_drains(self):
        previous = self.prepared()
        self.assertEqual(previous['status'], 'continuation_required')
        subject.validate_continuation(previous, HEAD, 100)
        self.journal = Path(self.temp.name) / 'next-journal'
        self.api = FakeAPI(self.journal)
        self.refresh.side_effect = [summary(9), summary(0)]
        result = self.controller(parent_run_id='101', continuation=previous).run(summary(9))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['continued_from_run_id'], '100')
        self.assertEqual(result['accepted_continuation_id'], previous['continuation']['id'])
        self.assertEqual(result['formats']['epub']['state'], 'complete')
        self.assertEqual(len(self.api.posts), 2)

    def test_round_robin_and_queue_rotation_across_budget(self):
        def inventory(epub, jpg, mp3, md):
            data = summary(epub)
            for ext, count in (('jpg', jpg), ('mp3', mp3), ('md', md)):
                row = next(row for row in data['formats'] if row['extension'] == ext)
                row.update(sources=30, not_mapped=count, definite_backlog=count,
                           source_content_unchanged=30-count)
            return data
        states = [inventory(10, 10, 10, 10), inventory(9, 10, 10, 10), inventory(9, 9, 10, 10)]
        self.refresh.side_effect = states
        result = self.controller(parent_run_id='100', max_batches=2).run(states[0])
        self.assertEqual(result['status'], 'continuation_required')
        self.assertEqual([inputs['extension'] for _, inputs in self.api.posts[::2]], ['epub', 'jpg'])
        self.assertEqual(result['remaining_formats'], ['mp3', 'md', 'epub', 'jpg'])
        self.journal = Path(self.temp.name) / 'next-journal'; self.api = FakeAPI(self.journal)
        self.refresh.side_effect = [states[-1], inventory(9, 9, 9, 10)]
        resumed = self.controller(parent_run_id='101', continuation=result, max_batches=1).run(states[-1])
        self.assertEqual(resumed['status'], 'continuation_required')
        self.assertEqual(self.api.posts[0][1]['extension'], 'mp3')
        self.assertEqual(resumed['remaining_formats'], ['md', 'epub', 'jpg', 'mp3'])

    def test_safe_time_boundary_yields_but_unknown_dispatch_does_not(self):
        def refresh():
            if self.api.posts:
                self.clock.now = 45
                return summary(9)
            return summary(10)
        self.refresh.side_effect = refresh
        result = self.controller(parent_run_id='100').run(summary())
        self.assertEqual(result['status'], 'continuation_required')
        self.assertEqual(len(self.api.posts), 2)

    def test_source_change_and_legacy_scopes_survive_continuation(self):
        for ext in ('md', 'html'):
            for scope in ('explicit_source_changes', 'first_conversion_only'):
                with self.subTest(ext=ext, scope=scope):
                    self.journal = Path(self.temp.name) / (ext + scope)
                    self.api = FakeAPI(self.journal)
                    before, after, final = [summary(n, ext, not_mapped=0, source_content_changed=n,
                                                   conversion_dependencies_checked=False) for n in (10, 9, 0)]
                    self.refresh.side_effect = [before, after]
                    previous = self.controller(parent_run_id='100', max_batches=1).run(before)
                    self.assertEqual(previous['status'], 'continuation_required')
                    previous['formats'][ext]['completion_scope'] = scope
                    subject.validate_continuation(previous, HEAD, 100)
                    with self.assertRaises(ValueError):
                        subject.validate_continuation(previous, 'b' * 40, 100)
                    self.journal = Path(self.temp.name) / ('next-' + ext + scope)
                    self.api = FakeAPI(self.journal)
                    self.refresh.side_effect = [after, final]
                    result = self.controller(parent_run_id='101', continuation=previous).run(after)
                    self.assertEqual(result['status'], 'complete')
                    self.assertEqual(result['formats'][ext]['state'], 'complete')
                    self.assertEqual(result['formats'][ext]['completion_scope'], scope)
                    self.assertEqual(result['formats'][ext]['dependency_freshness'], 'unknown')
                    self.assertFalse(result['formats'][ext]['conversion_dependencies_checked'])
                    self.assertEqual(self.api.posts[0][1]['expected_sha'], HEAD)
                    self.assertNotIn(ext, result['blocked'])

    def test_drift_or_unresolved_receipts_refuse_continuation(self):
        previous = self.prepared()
        for edit in (lambda data: data.update(expected_sha='b'*40),
                     lambda data: data['operations'][0].update(status='dispatch_unknown'),
                     lambda data: data['operations'][-1]['verification'].update(generation=NEXT),
                     lambda data: data.update(remaining_formats=[]),
                     lambda data: data['current_summary']['formats'][0].update(pending_shard_checkpoint=1)):
            data = copy.deepcopy(previous); edit(data)
            with self.assertRaises((ValueError, RuntimeError)): subject.validate_continuation(data, HEAD, 100)
        self.journal = Path(self.temp.name) / 'next-journal'; self.api = FakeAPI(self.journal)
        changed = summary(9); changed['search_generation'] = NEXT
        self.refresh.side_effect = [changed]
        result = self.controller(parent_run_id='101', continuation=previous).run(changed)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('search_generation changed', result['error'])
        self.assertFalse(self.api.posts)

    def test_pending_elsewhere_blocks_safe_continuation(self):
        before = summary(10); after = summary(9)
        for data in (before, after):
            row = next(row for row in data['formats'] if row['extension'] == 'jpg')
            row.update(sources=1, source_content_unchanged=1, pending_shard_checkpoint=1)
        self.refresh.side_effect = [before, after]
        result = self.controller(parent_run_id='100', max_batches=1).run(before)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('pending', result['error'])

    def api_for_continuation(self, previous):
        def run(identifier, **changes):
            return {'id': identifier, 'name': subject.DAILY_NAME, 'path': '.github/workflows/' + subject.DAILY,
                    'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': HEAD,
                    'run_attempt': 1, 'status': 'completed', 'conclusion': 'success',
                    'display_title': 'reader-format-continuation/' + previous['continuation']['id'],
                    'run_started_at': '2026-11-15T10:00:00Z', **changes}
        parent, child = run(100), run(101, status='in_progress', conclusion=None)
        operations = {op['run_id']: {'id': op['run_id'], 'display_title': op['run_name'],
                      'path': '.github/workflows/' + op['workflow'], 'event': 'workflow_dispatch',
                      'head_branch': 'main', 'head_sha': HEAD, 'run_attempt': 1,
                      'status': 'completed', 'conclusion': 'success'} for op in previous['operations']}
        session = Mock(); session.headers = {}
        api = subject.GitHub('fake', session=session)
        api.get_run = Mock(side_effect=lambda identifier: copy.deepcopy({100: parent, 101: child, **operations}[identifier]))
        api.request = Mock(return_value=parent)
        api.find_operation = Mock(return_value=[child])
        api.check_main = Mock()
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name in ('result.json', 'operations.jsonl'):
                archive.writestr(name, json.dumps(previous))
        api.report_artifact = Mock(return_value=(output.getvalue(), {'id': 55}, 'sha256:' + 'a'*64))
        return api, parent, child, operations

    def test_actual_github_run_name_uses_evaluated_title_for_parent_and_child(self):
        previous = self.prepared()
        api, parent, child, _operations = self.api_for_continuation(previous)
        parent.update(name='Reader format backlog / workflow_dispatch',
                      display_title='Reader format backlog / workflow_dispatch')
        child['name'] = child['display_title']
        self.assertEqual(api.continuation_report(100, previous['continuation']['id'], HEAD, '101'), previous)
        for field,value in [('name','foreign workflow'),('display_title','Reader format backlog / push'),
                            ('path','.github/workflows/other.yml'),('run_attempt',2)]:
            bad={**parent,field:value}
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                subject.validate_daily_run(bad, HEAD, completed=True)

    def test_continuation_artifact_binds_parent_success_unique_child_and_operation_attempts(self):
        previous = self.prepared()
        api, parent, child, operations = self.api_for_continuation(previous)
        loaded = api.continuation_report(100, previous['continuation']['id'], HEAD, '101')
        self.assertEqual(loaded, previous)
        self.assertEqual(api.report_artifact.call_args.args[1], 'reader-format-daily-100')
        for obj, field, value in ((parent, 'conclusion', 'failure'), (parent, 'run_attempt', 2),
                                  (parent, 'head_sha', 'b'*40), (parent, 'event', 'push'),
                                  (child, 'run_attempt', 2), (operations[1], 'run_attempt', 2)):
            old = obj[field]; obj[field] = value
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, RuntimeError)):
                api.continuation_report(100, previous['continuation']['id'], HEAD, '101')
            obj[field] = old
        api.find_operation.return_value = [child, child]
        with self.assertRaisesRegex(RuntimeError, 'unique'):
            api.continuation_report(100, previous['continuation']['id'], HEAD, '101')

    def test_artifact_result_journal_mismatch_or_wrong_continuation_id_rejected(self):
        previous = self.prepared()
        api, _parent, _child, _operations = self.api_for_continuation(previous)
        with self.assertRaisesRegex(ValueError, 'ID differs'):
            api.continuation_report(100, '0'*32, HEAD, '101')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('result.json', json.dumps(previous))
            archive.writestr('operations.jsonl', json.dumps({**previous, 'status': 'complete'}))
        api.report_artifact.return_value = (output.getvalue(), {'id': 55}, 'digest')
        with self.assertRaisesRegex(ValueError, 'journal differ'):
            api.continuation_report(100, previous['continuation']['id'], HEAD, '101')

    def test_preexisting_continuation_is_observed_without_another_post(self):
        previous = self.prepared()
        api, parent, child, _operations = self.api_for_continuation(previous)
        parent.update(status='in_progress', conclusion=None)
        api.dispatch = Mock()
        result = subject.continue_dispatch(api=api, result=previous,
                    journal=Path(self.temp.name)/'dispatch.json', expected_sha=HEAD,
                    current_run_id='100', clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['run_id'], child['id'])
        api.dispatch.assert_not_called()

    def test_continuation_dispatch_unknown_posts_once_and_does_not_wait_for_child(self):
        previous = self.prepared()
        api, parent, child, _operations = self.api_for_continuation(previous)
        parent.update(status='in_progress', conclusion=None)
        api.find_operation.side_effect = [[], [child]]
        api.active_children = Mock(return_value=[])
        api.dispatch = Mock(side_effect=TimeoutError('accepted but response lost'))
        journal = Path(self.temp.name) / 'dispatch.json'
        result = subject.continue_dispatch(api=api, result=previous, journal=journal, expected_sha=HEAD,
                                          current_run_id='100', clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['run_id'], 101)
        api.dispatch.assert_called_once_with(subject.DAILY,
            {'apply': 'true', 'known_writers_coordinated': 'true', 'continuation_run_id': '100',
             'continuation_id': previous['continuation']['id']})
        api.find_operation.side_effect = None; api.find_operation.return_value = []
        result = subject.continue_dispatch(api=api, result=previous, journal=journal, expected_sha=HEAD,
                                          current_run_id='100', clock=self.clock, sleep=self.clock.sleep,
                                          timeout_seconds=1)
        self.assertEqual(result['status'], 'dispatch_unknown')
        self.assertEqual(api.dispatch.call_count, 1)

    def test_definitive_continuation_rejection_fails_without_retry(self):
        previous = self.prepared()
        for code in (403, 422):
            api, parent, _child, _operations = self.api_for_continuation(previous)
            parent.update(status='in_progress', conclusion=None)
            api.find_operation = Mock(return_value=[])
            api.active_children = Mock(return_value=[])
            api.dispatch = Mock(side_effect=subject.DispatchRejected(str(code)))
            journal = Path(self.temp.name) / ('rejected-' + str(code) + '.json')
            with self.assertRaises(subject.DispatchRejected):
                subject.continue_dispatch(api=api, result=previous, journal=journal,
                    expected_sha=HEAD, current_run_id='100', clock=self.clock, sleep=self.clock.sleep)
            api.dispatch.assert_called_once()
            self.assertEqual(json.loads(journal.read_text())['status'], 'dispatch_rejected')


def publication_zip(publication=None, verification=None, extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('publication.json', json.dumps(publication or {'applied': True, 'generation': READER}))
        archive.writestr('format-automation-verification.json', json.dumps(verification or receipt()))
        for name, data in extra:
            archive.writestr(name, data)
    return output.getvalue()


class PublicationArtifactTests(unittest.TestCase):
    def setUp(self):
        self.operation = {'workflow': subject.PUBLISH, 'run_id': 7, 'run_name': 'reader-auto/' + 'f'*32,
                          'automation_id': 'f'*32}
        self.run = {'id': 7, 'head_sha': HEAD, 'head_branch': 'main', 'event': 'workflow_dispatch',
                    'run_attempt': 1, 'path': '.github/workflows/' + subject.PUBLISH,
                    'display_title': self.operation['run_name'], 'status': 'completed', 'conclusion': 'success',
                    'run_started_at': '2026-11-15T10:00:00Z'}
        self.raw = publication_zip()
        self.artifact = {'id': 99, 'name': subject.PUBLICATION_ARTIFACT, 'expired': False,
                         'size_in_bytes': len(self.raw), 'digest': 'sha256:' + hashlib.sha256(self.raw).hexdigest(),
                         'workflow_run': {'id': 7, 'head_sha': HEAD, 'head_branch': 'main'},
                         'created_at': '2026-11-15T10:01:00Z'}
        session = Mock(); session.headers = {}
        self.api = subject.GitHub('fake', session=session)
        self.api.get_run = Mock(side_effect=lambda _id: copy.deepcopy(self.run))
        self.api.request = Mock(side_effect=lambda _method, path, **_kwargs:
                                copy.deepcopy(self.run) if path.endswith('/attempts/1') else
                                {'total_count': 1, 'artifacts': [copy.deepcopy(self.artifact)]})
        self.api.download_artifact = Mock(side_effect=lambda _id: self.raw)

    def report(self): return self.api.publication_report(self.operation, HEAD)

    def replace_zip(self, raw):
        self.raw = raw
        self.artifact.update(size_in_bytes=len(raw), digest='sha256:' + hashlib.sha256(raw).hexdigest())

    def test_exact_run_artifact_and_attempt_are_bound(self):
        result = self.report()
        self.assertEqual(result['generation'], READER)
        self.assertEqual(result['run_id'], 7)
        self.assertEqual(result['run_attempt'], 1)
        self.assertEqual(result['artifact_id'], 99)
        self.assertEqual(result['automation_id'], self.operation['automation_id'])
        self.api.download_artifact.assert_called_once_with(99)
        self.assertEqual(self.api.get_run.call_count, 2)
        self.assertEqual(self.api.request.call_args_list[0].args[1], '/actions/runs/7/attempts/1')
        self.assertEqual(self.api.request.call_args_list[1].args[1], '/actions/runs/7/artifacts')

    def test_report_generation_mismatch_and_incomplete_consumers(self):
        for verification in (receipt(NEXT), {**receipt(), 'complete': False},
                             {**receipt(), 'complete': 'true'}, {**receipt(), 'consumers': {}}):
            with self.subTest(verification=verification):
                self.replace_zip(publication_zip(verification=verification))
                with self.assertRaisesRegex(RuntimeError, 'strict verify-generation'): self.report()
        self.replace_zip(publication_zip(publication={'applied': False, 'generation': READER}))
        with self.assertRaisesRegex(RuntimeError, 'did not apply'): self.report()

    def test_foreign_replayed_expired_and_oversized_artifacts(self):
        original = copy.deepcopy(self.artifact)
        for change in ({'workflow_run': {'id': 8, 'head_sha': HEAD, 'head_branch': 'main'}},
                       {'workflow_run': {'id': 7, 'head_sha': 'b'*40, 'head_branch': 'main'}},
                       {'created_at': '2026-11-15T09:59:59Z'}, {'expired': True},
                       {'size_in_bytes': subject.MAX_ARCHIVE + 1}):
            with self.subTest(change=change):
                self.artifact = {**original, **change}
                with self.assertRaisesRegex(RuntimeError, 'publication artifact'): self.report()
        self.api.download_artifact.assert_not_called()

    def test_rerun_or_replaced_run_after_download_rejected(self):
        for change in ({'run_attempt': 2}, {'id': 8}, {'head_sha': 'b'*40}):
            with self.subTest(change=change):
                self.api.get_run.side_effect = [self.run, {**self.run, **change}]
                with self.assertRaises(RuntimeError): self.report()

    def test_digest_missing_or_changed_rejected(self):
        for digest in (None, 'sha256:' + '0'*64):
            self.artifact['digest'] = digest
            with self.assertRaisesRegex(RuntimeError, 'digest'): self.report()

    def test_missing_duplicate_or_incomplete_inventory_rejected(self):
        for inventory in ({'total_count': 0, 'artifacts': []},
                          {'total_count': 2, 'artifacts': [self.artifact, self.artifact]},
                          {'total_count': 101, 'artifacts': [self.artifact]}):
            self.api.request.side_effect = [self.run, inventory]
            with self.assertRaises(RuntimeError): self.report()
        self.api.download_artifact.assert_not_called()

    def test_archive_paths_symlinks_duplicates_and_bombs_rejected(self):
        symlink = zipfile.ZipInfo('link.json'); symlink.external_attr = 0o120777 << 16
        for name, data in (('../publication.json', '{}'), ('/other.json', '{}'),
                           ('dir/other.json', '{}'), ('dir\\other.json', '{}'),
                           ('publication.json', '{}'), (symlink, 'publication.json'),
                           ('big.json', 'x' * (subject.MAX_REPORT + 1))):
            with self.subTest(name=name):
                with self.assertWarns(UserWarning) if name == 'publication.json' else nullcontext():
                    raw = publication_zip(extra=[(name, data)])
                with self.assertRaises(ValueError): subject.read_publication_archive(raw)
        with patch.object(subject, 'MAX_EXPANDED', 1):
            with self.assertRaisesRegex(ValueError, 'expands'): subject.read_publication_archive(self.raw)
        with patch.object(subject, 'MAX_ARCHIVE', 1):
            with self.assertRaisesRegex(ValueError, 'size'): subject.read_publication_archive(self.raw)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive: archive.writestr('publication.json', '{}')
        with self.assertRaisesRegex(ValueError, 'missing'): subject.read_publication_archive(output.getvalue())

    def test_download_bounds_and_never_sends_token_to_storage(self):
        redirect = Mock(status_code=302, headers={'Location': 'https://test.blob.core.windows.net/signed'})
        redirect.__enter__ = Mock(return_value=redirect); redirect.__exit__ = Mock(return_value=False)
        self.api.session.request.return_value = redirect
        download = Mock(status_code=200, headers={})
        download.__enter__ = Mock(return_value=download); download.__exit__ = Mock(return_value=False)
        download.iter_content.return_value = [b'123', b'456']
        with patch('requests.get', return_value=download) as get, patch.object(subject, 'MAX_ARCHIVE', 5):
            with self.assertRaisesRegex(ValueError, 'oversized'): subject.GitHub.download_artifact(self.api, 99)
            self.assertNotIn('headers', get.call_args.kwargs)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
        for url in ('http://test.blob.core.windows.net/a', 'https://evil.test/a',
                    'https://user:pass@test.blob.core.windows.net/a'):
            redirect.headers['Location'] = url
            with self.assertRaisesRegex(RuntimeError, 'host'): subject.GitHub.download_artifact(self.api, 99)


class SourceGuardTests(unittest.TestCase):
    def setUp(self):
        from scripts import reader_format_backlog as backlog
        self.backlog = backlog
        self.revisions = {'VoiceOfML/Teachers': HEAD}
        self.pointer = backlog.search_publication_pointer.make_pointer(SEARCH, None, 'a'*32)
        self.hub = Mock()
        self.hub.read_bytes.side_effect = lambda _bucket, path: self.pointer if path == backlog.CURRENT else b'manifest'
        self.api = Mock(); self.api.repo_info.return_value = SimpleNamespace(sha=HEAD)
        self.github = Mock()
        self.capture = Mock(return_value=(self.pointer, b'manifest', {}, self.revisions, {}))

    def guard(self, **changes):
        args = dict(generation=SEARCH, expected_sha=HEAD, revisions=self.revisions,
                    hub=self.hub, api=self.api, github=self.github, capture=self.capture, head=lambda: HEAD)
        args.update(changes)
        return subject.guard_source(**args)

    def test_guard_checks_heads_and_fetched_revisions_without_writes(self):
        result = self.guard()
        self.assertTrue(result['current_source_heads_checked'])
        self.assertEqual(result['remote_writes'], 0)
        self.api.repo_info.assert_called_once_with('VoiceOfML/Teachers', repo_type='dataset', timeout=30)
        self.assertEqual(self.github.check_main.call_count, 2)
        self.assertEqual(self.hub.read_bytes.call_count, 3)

    def test_guard_rejects_teacher_drift_and_fetched_state_mismatch(self):
        self.api.repo_info.return_value = SimpleNamespace(sha='b'*40)
        with self.assertRaisesRegex(ValueError, 'current source head'): self.guard()
        self.api.repo_info.return_value = SimpleNamespace(sha=HEAD)
        with self.assertRaisesRegex(ValueError, 'fetched state'):
            self.guard(revisions={'VoiceOfML/Teachers': 'b'*40})
        with self.assertRaisesRegex(ValueError, 'mapping'): self.guard(revisions={'repo': 'main'})
        with self.assertRaisesRegex(ValueError, 'checkout'): self.guard(head=lambda: 'b'*40)
        with self.assertRaisesRegex(ValueError, 'generation differs'): self.guard(generation=NEXT)

    def test_guard_rejects_pointer_manifest_drift_and_capture_failure(self):
        self.hub.read_bytes.side_effect = [self.pointer, b'changed']
        with self.assertRaisesRegex(ValueError, 'changed during source guard'): self.guard()
        self.hub.read_bytes.side_effect = [self.pointer, self.pointer, b'changed manifest']
        with self.assertRaisesRegex(ValueError, 'changed during source guard'): self.guard()
        self.hub.read_bytes.side_effect = None; self.hub.read_bytes.return_value = self.pointer
        self.capture.side_effect = ValueError('manifest identity differs')
        with self.assertRaisesRegex(ValueError, 'manifest identity'): self.guard()

    def test_default_capture_is_strict_backlog_capture(self):
        with patch.object(self.backlog, 'capture_search', self.capture):
            self.guard(capture=None)
        self.capture.assert_called_once_with(self.hub)


class GitHubTests(unittest.TestCase):
    def api(self):
        session = Mock(); session.headers = {}
        return subject.GitHub('fake-token', session=session)

    def test_get_503_recovers_on_third_attempt(self):
        api = self.api()
        unavailable = Mock(status_code=503)
        success = Mock(status_code=200)
        success.json.return_value = {'object': {'sha': HEAD}}
        api.session.request.side_effect = [unavailable, unavailable, success]
        with patch.object(subject.time, 'sleep') as sleep:
            api.check_main(HEAD)
        self.assertEqual(api.session.request.call_args_list, [
            call('GET', 'https://api.github.com/repos/anftm/pipeline/git/ref/heads/main',
                 timeout=30, allow_redirects=False)] * 3)
        self.assertEqual(unavailable.close.call_count, 2)
        unavailable.json.assert_not_called()
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_get_503_exhausts_three_attempts(self):
        api = self.api()
        api.session.request.return_value = Mock(status_code=503)
        with patch.object(subject.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'GitHub GET returned 503'):
                api.request('GET', '/actions/runs', params={'per_page': 1})
        self.assertEqual(api.session.request.call_count, 3)
        self.assertEqual(api.session.request.call_args_list, [api.session.request.call_args_list[0]] * 3)
        self.assertEqual(api.session.request.call_args.kwargs['params'], {'per_page': 1})
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_get_transport_errors_recover_or_exhaust(self):
        for kind in (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                     requests.exceptions.ChunkedEncodingError):
            for recover in (True, False):
                with self.subTest(kind=kind, recover=recover):
                    api = self.api()
                    error = kind('transport failed')
                    success = Mock(status_code=200)
                    success.json.return_value = {'id': 123}
                    api.session.request.side_effect = [error, error, success if recover else error]
                    with patch.object(subject.time, 'sleep') as sleep:
                        if recover:
                            self.assertEqual(api.get_run(123), {'id': 123})
                        else:
                            with self.assertRaises(kind) as raised:
                                api.get_run(123)
                            self.assertIs(raised.exception, error)
                    self.assertEqual(api.session.request.call_count, 3)
                    self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_permanent_get_failures_are_not_retried(self):
        for status in (301, 400, 401, 403, 404, 422, 501):
            with self.subTest(status=status):
                api = self.api()
                api.session.request.return_value = Mock(status_code=status)
                with patch.object(subject.time, 'sleep') as sleep:
                    with self.assertRaisesRegex(RuntimeError, 'GitHub GET returned ' + str(status)):
                        api.get_run(123)
                api.session.request.assert_called_once()
                sleep.assert_not_called()
        for error in (requests.exceptions.SSLError('certificate verification failed'),
                      requests.exceptions.InvalidURL('invalid URL')):
            with self.subTest(error=type(error)):
                api = self.api()
                api.session.request.side_effect = error
                with patch.object(subject.time, 'sleep') as sleep:
                    with self.assertRaises(type(error)):
                        api.get_run(123)
                api.session.request.assert_called_once()
                sleep.assert_not_called()

    def test_get_payload_and_identity_failures_are_not_retried(self):
        for invalid_json in (True, False):
            with self.subTest(invalid_json=invalid_json):
                api = self.api()
                response = Mock(status_code=200)
                response.json.return_value = {'object': {'sha': 'b' * 40}}
                if invalid_json:
                    response.json.side_effect = ValueError('invalid JSON')
                api.session.request.return_value = response
                with patch.object(subject.time, 'sleep') as sleep:
                    with self.assertRaisesRegex(ValueError if invalid_json else RuntimeError,
                                                'invalid JSON' if invalid_json else 'differs from expected HEAD'):
                        api.check_main(HEAD)
                api.session.request.assert_called_once()
                sleep.assert_not_called()

    def test_post_statuses_are_never_retried(self):
        for status in (204, 302, 400, 401, 403, 404, 405, 409, 422, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                api = self.api()
                api.session.request.return_value = Mock(status_code=status)
                with patch.object(subject.time, 'sleep') as sleep:
                    if status == 204:
                        self.assertIsNone(api.dispatch('reader-ebook-stream.yml', {}))
                    else:
                        with self.assertRaises(RuntimeError) as raised:
                            api.dispatch('reader-ebook-stream.yml', {})
                        expected = subject.DispatchRejected if status in {400, 401, 403, 404, 405, 409, 422} else RuntimeError
                        self.assertIs(type(raised.exception), expected)
                api.session.request.assert_called_once()
                sleep.assert_not_called()

    def test_ambiguous_post_transport_errors_are_never_retried(self):
        for kind in (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                     requests.exceptions.ChunkedEncodingError):
            with self.subTest(kind=kind):
                api = self.api()
                error = kind('dispatch outcome unknown')
                api.session.request.side_effect = error
                with patch.object(subject.time, 'sleep') as sleep:
                    with self.assertRaises(kind) as raised:
                        api.dispatch('reader-ebook-stream.yml', {})
                self.assertIs(raised.exception, error)
                api.session.request.assert_called_once()
                sleep.assert_not_called()

    def test_dispatch_uses_main_and_makes_one_post(self):
        api = self.api(); api.session.request.side_effect = TimeoutError('unknown')
        with self.assertRaises(TimeoutError): api.dispatch('reader-ebook-stream.yml', {'automation_id': 'a'*32})
        self.assertEqual(api.session.request.call_count, 1)
        self.assertEqual(api.session.request.call_args.kwargs['json']['ref'], 'main')
        self.assertFalse(api.session.request.call_args.kwargs['allow_redirects'])

    def test_run_lookup_does_not_filter_away_wrong_branch_or_event(self):
        api = self.api(); api.runs = Mock(return_value=[{'display_title': 'reader-auto/one'},
                                                        {'display_title': 'reader-auto/other'}])
        matches = api.find_operation({'workflow': 'reader-image-stream.yml', 'run_name': 'reader-auto/one',
                                      'created_after': '2026-01-01T00:00:00Z'})
        self.assertEqual(len(matches), 1)
        self.assertEqual(set(api.runs.call_args.kwargs), {'created'})

    def test_inventory_pagination_and_oversized_inventory_fail_closed(self):
        api = self.api()
        api.request = Mock(side_effect=[{'total_count': 101, 'workflow_runs': [{'id': n} for n in range(100)]},
                                       {'total_count': 101, 'workflow_runs': [{'id': 100}]}])
        self.assertEqual(len(api.runs(status='queued')), 101)
        api.request = Mock(return_value={'total_count': 1001, 'workflow_runs': []})
        with self.assertRaisesRegex(RuntimeError, 'inventory'): api.runs()

    @patch.object(subject.time, 'sleep')
    def test_changed_run_inventory_restarts_reads_but_never_accepts_truncation(self, sleep):
        api = self.api()
        api.request = Mock(side_effect=[{'total_count': 2, 'workflow_runs': [{'id': 1}]},
                                       {'total_count': 1, 'workflow_runs': [{'id': 2}]}])
        self.assertEqual(api.runs(status='in_progress'), [{'id': 2}])
        self.assertEqual(api.request.call_count, 2)
        self.assertTrue(all(call.args[0] == 'GET' for call in api.request.call_args_list))
        api.request = Mock(return_value={'total_count': 2, 'workflow_runs': [{'id': 1}]})
        with self.assertRaisesRegex(RuntimeError, 'bounded reads'):
            api.runs(status='queued')
        self.assertEqual(api.request.call_count, 6)
        self.assertEqual(len({c.kwargs['params']['_observation'] for c in api.request.call_args_list}), 6)
        self.assertTrue(all(c.kwargs['headers']=={'Cache-Control':'no-cache'} for c in api.request.call_args_list))
        api.request = Mock(side_effect=[
            {'total_count': 101, 'workflow_runs': [{'id': n} for n in range(100)]},
            {'total_count': 100, 'workflow_runs': []},
            {'total_count': 1, 'workflow_runs': [{'id': 900}]}])
        self.assertEqual(api.runs(status='in_progress'), [{'id': 900}])
        self.assertEqual([call.kwargs['params']['page'] for call in api.request.call_args_list], [1, 2, 1])

    @patch.object(subject.time, 'sleep')
    def test_inventory_recovers_after_three_stale_reads_with_one_snapshot_per_pagination(self, sleep):
        api = self.api()
        api.request = Mock(side_effect=[{'total_count': 1, 'workflow_runs': []}]*3 + [
            {'total_count': 101, 'workflow_runs': [{'id': n} for n in range(100)]},
            {'total_count': 101, 'workflow_runs': [{'id': 100}]}])
        self.assertEqual(len(api.runs(status='in_progress')), 101)
        calls=api.request.call_args_list
        self.assertEqual(calls[-1].kwargs['params']['_observation'],calls[-2].kwargs['params']['_observation'])
        self.assertEqual([c.args[0] for c in calls], ['GET']*5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2,4,8])

    def test_waiting_same_group_successors_do_not_block_current_controller(self):
        api=subject.GitHub('fixture')
        api.runs=Mock(return_value=[{'id':2,'path':'.github/workflows/reader-format-daily.yml','status':'pending'},
                                   {'id':3,'path':'.github/workflows/reader-backlog-monitor.yml','status':'pending'},
                                   {'id':4,'path':'.github/workflows/reader-maintenance.yml','status':'pending'}])
        api.request=Mock(return_value={'total_count':0,'jobs':[]})
        self.assertEqual(api.active_children('1'),[])
        api.request.return_value={'total_count':1,'jobs':[{'status':'in_progress','steps':[{'name':'running'}],'started_at':'now'}]}
        self.assertEqual({r['id'] for r in api.active_children('1')},{2,3,4})
        api.request.return_value={'total_count':2,'jobs':[]}
        self.assertEqual({r['id'] for r in api.active_children('1')},{2,3,4})

    def test_active_detection_includes_pending_and_excludes_parent(self):
        api = self.api()
        api.request = Mock(return_value={'total_count': 1, 'jobs': [{'id': 100}]})
        api.runs = Mock(return_value=[{'id': 1, 'path': '.github/workflows/reader-format-daily.yml', 'status': 'in_progress'},
                                     {'id': 2, 'path': '.github/workflows/reader-ebook-stream.yml', 'status': 'pending'},
                                     {'id': 3, 'path': '.github/workflows/search-daily-sync.yml', 'status': 'queued'}])
        self.assertEqual({r['id'] for r in api.active_children('1')}, {2, 3})
        self.assertIn('pending', [call.kwargs['status'] for call in api.runs.call_args_list])
        api.request = Mock(return_value={'total_count': 0, 'jobs': []})
        self.assertEqual({r['id'] for r in api.active_children('1')}, {2})
        api.request = Mock(side_effect=RuntimeError('unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            api.active_children('1')


if __name__ == '__main__':
    unittest.main()
