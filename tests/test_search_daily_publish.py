"""Offline integration: real orchestration, transitions, validators and adapters.

Only source-head lookup and external transports are replaced. This cannot prove
live HF/GitHub/S3 semantics, writer isolation, or production-scale timing.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import search_daily_publish as subject
from scripts import search_generation, search_artifact_store, search_adopt
from scripts import search_transition as tx, search_snapshot
from scripts.search_publication_pointer import make_pointer
from tests.test_search_artifact_store import MemoryStore as ArtifactStore
from tests.test_search_transition import MemoryStore, receipt, default_receipt

PAGES_SHA, SPACE_SHA = 'd' * 40, 'e' * 40
TIME = '2026-11-15T00:00:00Z'


class Response:
    links = {}

    def __init__(self, body=None, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP failure')


class Remote:
    """Small stateful HF/GitHub/bucket/consumer transport, not function mocks."""
    def __init__(self, store, objects, parent):
        self.store, self.objects = store, objects
        self.space_generation = self.pages_generation = self.default = parent
        self.loaded_space = parent
        self.space_sha, self.pages_sha = SPACE_SHA, PAGES_SHA
        self.runtime_sha = SPACE_SHA
        self.stages = []
        self.run = None
        self.run_observations = 0
        self.run_delay = 0
        self.conclusion = 'success'
        self.loss = set()
        self.no_effect = set()
        self.calls = []
        self.after_dispatch = None
        self.bad_consumer = False

    def read_bytes(self, bucket, key):
        assert bucket == search_adopt.BUCKET
        return self.store.pointer if key == 'published/current.json' else self.objects[key]

    def get_space_variables(self, _space):
        return {subject.finalize.VARIABLE: SimpleNamespace(value=self.space_generation, updated_at=TIME)}

    def space_info(self, _space, timeout):
        return SimpleNamespace(sha=self.space_sha)

    def get_space_runtime(self, _space):
        stage = self.stages[0] if self.stages else 'RUNNING'
        return SimpleNamespace(stage=stage, raw={'sha': self.runtime_sha})

    def add_space_variable(self, _space, *, key, value):
        self.calls.append('space-write')
        if 'space' not in self.no_effect:
            self.space_generation = self.loaded_space = value
        if 'space' in self.loss:
            raise TimeoutError('Space response lost')

    def get(self, url, *, timeout, params=None):
        base = subject.pages.deploy.BASE
        if url == base + '/branches/main':
            return Response({'commit': {'sha': self.pages_sha}})
        if url == base + '/actions/variables/' + subject.finalize.VARIABLE:
            return Response({'name': subject.finalize.VARIABLE, 'value': self.default})
        if url == base + '/actions/workflows/static.yml/runs':
            if params.get('status'):
                runs = [self.run] if self.run and self.run['status'] == params['status'] else []
            else:
                self.run_observations += 1
                runs = [self.run] if self.run and self.run_observations > self.run_delay else []
            return Response({'workflow_runs': runs})
        raise AssertionError(url)

    def post(self, url, *, timeout, json):
        self.calls.append('pages-dispatch')
        assert url.endswith('/dispatches') and json['ref'] == 'main'
        pending = tx.load(self.store).record['pending']
        assert pending['write_claim'] and pending['expected_revision'] == self.pages_sha
        assert pending['operation_id'] == json['inputs']['search_transition']
        if 'pages' not in self.no_effect:
            self.run = {'id': 1, 'run_attempt': 1, 'event': 'workflow_dispatch', 'head_branch': 'main',
                        'head_sha': self.pages_sha, 'path': '.github/workflows/static.yml',
                        'created_at': pending['created_at'], 'display_title': subject.pages.deploy.run_name(pending['operation_id']),
                        'status': 'completed', 'conclusion': self.conclusion}
            if self.conclusion == 'success':
                self.pages_generation = json['inputs']['search_generation']
        if self.after_dispatch:
            self.after_dispatch()
        if 'pages' in self.loss:
            raise TimeoutError('Pages response lost')
        return Response(status=204)

    def patch(self, url, *, timeout, json):
        self.calls.append('default-write')
        assert tx.load(self.store).record['pending']['write_claim']
        if 'default' not in self.no_effect:
            self.default = json['value']
        if 'default' in self.loss:
            raise TimeoutError('default response lost')
        return Response(status=204)

    def public_get(self, url, limit):
        if url.startswith(subject.daily.BUCKET_RESOLVE):
            raw = self.read_bytes(search_adopt.BUCKET, url.removeprefix(subject.daily.BUCKET_RESOLVE))
        elif url == search_adopt.SPACE + '/api/search-data-generation':
            manifest = json.loads(self.objects[f'generations/{self.loaded_space}/manifest.json'])
            raw = search_snapshot.canonical({'generation': self.loaded_space, 'verified': not self.bad_consumer,
                                             'surface': 'space', 'records': manifest['record_count'],
                                             'files': len(manifest['objects']['space']), 'api_generation': 'api:current'})
        elif url.startswith(search_adopt.PAGES + '/data/'):
            name = url.removeprefix(search_adopt.PAGES + '/data/')
            manifest = json.loads(self.objects[f'generations/{self.pages_generation}/manifest.json'])
            raw = (search_snapshot.canonical(manifest) if name == 'generation-manifest.json'
                   else self.objects[manifest['objects']['pages'][name]['object']])
        else:
            raise AssertionError(url)
        assert len(raw) <= limit
        return raw


class DailyPublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build two real immutable generations once; each test gets its own copy.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / name for name in ('source', 'tree', 'browser', 'revisions')]
            paths[1].write_bytes(search_snapshot.canonical({'VoiceOfML/Test': []}))
            paths[2].write_bytes(search_snapshot.canonical({'VoiceOfML/Test': {'': {'d': [], 'f': []}}}))
            paths[3].write_bytes(search_snapshot.canonical({'VoiceOfML/Test': 'a' * 40}))
            artifacts = ArtifactStore()
            generations = []
            for name in ('Parent', 'Candidate'):
                paths[0].write_bytes(search_snapshot.canonical({'v': 2, 'rp': ['VoiceOfML/Test'], 'fd': [[]],
                                                               'rc': [[0, name, 'pdf', 0, 4, 0]]}))
                output = root / name
                result = search_generation.build_generation(*paths, output)
                search_artifact_store.stage_generation(output, artifacts)
                generations.append(result['generation'])
            cls.parent, cls.candidate = generations
            cls.objects = artifacts.files

    def setUp(self):
        self.store = MemoryStore()
        old = 'a' * 64
        c = tx.begin(self.store, parent_pointer=self.store.pointer, candidate=self.parent,
                     source_revisions_sha256='c' * 64, transition='1' * 32,
                     pages_receipt=receipt('pages', old), space_receipt=receipt('space', old),
                     pages_default=default_receipt('pages', old), space_default=default_receipt('space', old))
        for site in ('space', 'pages'):
            kw = {'config_before': {'generation': old, 'updated_at': TIME}} if site == 'space' else {}
            c = tx.start_site(self.store, c, site=site, operation_id='2' * 32,
                              expected_revision=SPACE_SHA if site == 'space' else PAGES_SHA, created_at=TIME, **kw)
            c = tx.observe_site(self.store, c, site=site, generation=self.parent, receipt=receipt(site, self.parent))
        evidence = dict(pages_receipt=receipt('pages', self.parent), space_receipt=receipt('space', self.parent))
        c = tx.mark_publishing(self.store, c, **evidence)
        c = tx.publish_pointer(self.store, self.store, c, **evidence)
        for site in ('space', 'pages'):
            c = tx.start_default(self.store, c, site=site, operation_id='3' * 32,
                                 expected_revision=SPACE_SHA if site == 'space' else PAGES_SHA, created_at=TIME)
            c = tx.observe_default(self.store, c, site=site, receipt=default_receipt(site, self.parent))
        c = tx.finish(self.store, c, **evidence, pages_default=default_receipt('pages', self.parent),
                      space_default=default_receipt('space', self.parent))
        self.assertEqual(c.record['sequence'], 11)
        self.store.pointer_writes = 0
        self.initial_head = self.store.head
        self.remote = Remote(self.store, dict(self.objects), self.parent)
        self.elapsed = 0
        self.events = []
        for p in (patch.object(subject.promotion_intent.preflight.fetch, 'REPOS', ['VoiceOfML/Test']),
                  patch.object(subject.promotion_intent.preflight.fetch, 'get_repo_sha', return_value='a' * 40)):
            p.start()
            self.addCleanup(p.stop)

    def sleep(self, seconds):
        self.elapsed += seconds
        if self.remote.stages:
            self.remote.stages.pop(0)

    def run_publish(self, **changes):
        options = dict(generation=self.candidate, expected_parent=self.parent, pages_revision=PAGES_SHA,
                       space_revision=SPACE_SHA, known_writers_coordinated=True, timeout_seconds=20,
                       store=self.store, pointer_store=self.store, hub=self.remote, session=self.remote,
                       api=self.remote, get=self.remote.public_get, revision=lambda *_: 'a' * 40,
                       clock=lambda: self.elapsed, sleep=self.sleep, emit=self.events.append)
        return subject.publish(**{**options, **changes})

    def cli_args(self):
        return ['--generation', self.candidate, '--expected-parent', self.parent,
                '--pages-revision', PAGES_SHA, '--space-revision', SPACE_SHA,
                '--known-writers-coordinated', '--timeout-seconds', '3600']

    def test_cli_wires_exact_arguments_into_the_real_complete_chain(self):
        output = []
        with patch.dict(os.environ, {'PAGES_TOKEN': 'offline-test'}), \
                patch.object(subject.tx, 'S3TransitionStore', return_value=self.store), \
                patch.object(subject, 'S3SearchPointerStore', return_value=self.store), \
                patch.object(subject.space, 'HubBucketStore', return_value=self.remote), \
                patch.object(subject.requests, 'Session', return_value=self.remote), \
                patch('huggingface_hub.HfApi', return_value=self.remote), \
                patch.object(subject, '_emit', side_effect=output.append):
            self.remote.headers = {}
            real_publish = subject.publish
            def offline_publish(**kwargs):
                self.assertEqual(kwargs['timeout_seconds'], 3600)
                return real_publish(**kwargs, get=self.remote.public_get, emit=self.events.append)
            with patch.object(subject, 'publish', side_effect=offline_publish):
                self.assertEqual(subject.main(self.cli_args()), 0)
        self.assertEqual(output[-1]['kind'], 'search-daily-publish-complete')
        self.assertEqual(output[-1]['generation'], self.candidate)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])

    def test_cli_blocked_result_is_nonzero_and_does_not_claim_completion(self):
        output = []
        with patch.dict(os.environ, {'PAGES_TOKEN': 'offline-test'}), \
                patch.object(subject.tx, 'S3TransitionStore', return_value=self.store), \
                patch.object(subject, 'S3SearchPointerStore', return_value=self.store), \
                patch.object(subject.space, 'HubBucketStore', return_value=self.remote), \
                patch('huggingface_hub.HfApi', return_value=self.remote), \
                patch.object(subject, 'publish', side_effect=subject.Blocked('no progress')), \
                patch.object(subject, '_emit', side_effect=output.append):
            self.assertEqual(subject.main(self.cli_args()), 2)
        self.assertEqual(output[-1]['status'], 'blocked')
        self.assertTrue(output[-1]['journal_retained'])
        self.assertEqual(self.store.head, self.initial_head)

    def test_real_normal_path_audits_candidate_and_orders_once_writes(self):
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['published']['generation'], self.candidate)
        self.assertEqual(result['journal']['generation'], self.candidate)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])
        self.assertEqual(self.store.pointer_writes, 1)
        records = [json.loads(raw) for raw in self.store.records.values()]
        own = sorted((r for r in records if r['transition'] == result['transaction']), key=lambda r: r['sequence'])
        self.assertEqual(own[0]['state'], 'prepared')
        self.assertEqual(own[-1]['state'], 'complete')
        pending = [r['pending'] for r in own if r['pending']]
        self.assertEqual({p['expected_revision'] for p in pending if p['site'] == 'space'}, {SPACE_SHA})
        self.assertEqual({p['expected_revision'] for p in pending if p['site'] == 'pages'}, {PAGES_SHA})
        self.assertEqual(len({p['operation_id'] for p in pending}), 4)
        self.assertEqual(sum(p['write_claim'] is not None for p in pending), 3)
        self.assertGreater(tx.audit_history(self.store, tx.load(self.store)), len(own))
        self.assertFalse(result['resume_supported'])

    def test_all_lost_write_responses_are_observed_without_replay(self):
        self.remote.loss = {'space', 'pages', 'default'}
        self.store.lose_pointer_response = True
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])
        self.assertEqual(self.store.pointer_writes, 1)
        self.assertEqual(sum(e['kind'] == 'search-daily-publish-unknown' for e in self.events), 3)

    def test_parent_without_progress_blocks_and_second_start_refuses_resume(self):
        self.remote.loss = self.remote.no_effect = {'space'}
        with self.assertRaisesRegex(subject.Blocked, 'space-parent-configured'):
            self.run_publish(timeout_seconds=6)
        c = tx.load(self.store)
        self.assertTrue(c.record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write'])
        with self.assertRaisesRegex(ValueError, 'unfinished work'):
            self.run_publish()
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(tx.load(self.store), c)

    def test_unknown_dispatch_not_visible_blocks_without_redispatch(self):
        self.remote.loss = self.remote.no_effect = {'pages'}
        with self.assertRaisesRegex(subject.Blocked, 'pages-run-not-visible'):
            self.run_publish(timeout_seconds=6)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_default_204_then_parent_then_candidate_waits_without_repatch(self):
        original_get = self.remote.get
        readbacks = []
        def delayed_get(url, **kwargs):
            if url.endswith('/actions/variables/' + subject.finalize.VARIABLE) and 'default-write' in self.remote.calls:
                value = self.parent if len(readbacks) < 2 else self.candidate
                readbacks.append(value)
                return Response({'name': subject.finalize.VARIABLE, 'value': value})
            return original_get(url, **kwargs)
        self.remote.get = delayed_get
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(readbacks[:3], [self.parent, self.parent, self.candidate])
        self.assertEqual(self.elapsed, 10)
        self.assertEqual(self.remote.calls.count('default-write'), 1)
        self.assertEqual(sum(e.get('status') == 'pages-default-parent-visible' for e in self.events), 2)
        self.assertEqual(result['journal']['generation'], self.candidate)

    def test_default_204_then_foreign_readback_fails_without_wait_or_repatch(self):
        original_get = self.remote.get
        def foreign_get(url, **kwargs):
            if url.endswith('/actions/variables/' + subject.finalize.VARIABLE) and 'default-write' in self.remote.calls:
                return Response({'name': subject.finalize.VARIABLE, 'value': 'f' * 64})
            return original_get(url, **kwargs)
        self.remote.get = foreign_get
        with self.assertRaisesRegex(ValueError, 'foreign generation'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(self.remote.calls.count('default-write'), 1)
        current = tx.load(self.store)
        self.assertEqual(current.record['state'], 'pointer-observed')
        self.assertTrue(current.record['pending']['write_claim'])
        self.assertEqual(current.record['defaults']['pages']['generation'], self.parent)

    def test_default_remaining_parent_blocks_without_repatch(self):
        self.remote.loss = self.remote.no_effect = {'default'}
        with self.assertRaisesRegex(subject.Blocked, 'pages-default-parent-visible'):
            self.run_publish(timeout_seconds=6)
        self.assertEqual(self.remote.calls.count('default-write'), 1)
        self.assertEqual(tx.load(self.store).record['state'], 'pointer-observed')
        self.assertTrue(tx.load(self.store).record['pending']['write_claim'])

    def test_known_build_stages_wait_then_full_checks_still_run(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            self.remote.stages = ['RUNNING_BUILDING', 'BUILDING', 'APP_STARTING']
        self.remote.add_space_variable = write
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        waits = [e['status'] for e in self.events if e['kind'] == 'search-daily-publish-wait']
        self.assertEqual(waits, ['space-RUNNING_BUILDING', 'space-BUILDING', 'space-APP_STARTING'])
        self.assertEqual(self.elapsed, 15)

    def test_build_completion_does_not_skip_actual_consumer_checks(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            self.remote.stages = ['BUILDING']
            self.remote.bad_consumer = True
        self.remote.add_space_variable = write
        with self.assertRaisesRegex(ValueError, 'Space has not verified'):
            self.run_publish()
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.elapsed, 5)

    def test_successful_dispatch_with_failed_run_stops_before_pointer(self):
        self.remote.conclusion = 'failure'
        with self.assertRaisesRegex(ValueError, 'deployment failed'):
            self.run_publish()
        self.assertEqual(self.store.pointer_writes, 0)
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch'])

    def test_delayed_run_visibility_is_progress_not_success(self):
        self.remote.run_delay = 3
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.elapsed, 10)
        self.assertEqual(self.remote.calls.count('pages-dispatch'), 1)

    def test_bound_arguments_reject_before_intent(self):
        for change in ({'expected_parent': 'f' * 64}, {'pages_revision': 'f' * 40},
                       {'space_revision': 'f' * 40}, {'known_writers_coordinated': False},
                       {'pages_revision': 'main'}, {'space_revision': 'e' * 39},
                       {'timeout_seconds': float('nan')}, {'timeout_seconds': 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_publish(**change)
            self.assertEqual(self.store.head, self.initial_head)
            self.assertFalse(self.remote.calls)

    def test_space_revision_drift_after_dispatch_blocks_finalize(self):
        self.remote.after_dispatch = lambda: setattr(self.remote, 'space_sha', 'f' * 40)
        with self.assertRaisesRegex(ValueError, 'Space repository differs'):
            self.run_publish()
        self.assertEqual(self.store.pointer_writes, 0)

    def test_pages_revision_drift_after_dispatch_blocks_acceptance(self):
        self.remote.after_dispatch = lambda: setattr(self.remote, 'pages_sha', 'f' * 40)
        with self.assertRaisesRegex(ValueError, 'Pages revision differs'):
            self.run_publish()
        self.assertEqual(self.store.pointer_writes, 0)

    def test_corrupt_candidate_never_starts_or_writes(self):
        manifest = json.loads(self.remote.objects[f'generations/{self.candidate}/manifest.json'])
        self.remote.objects[manifest['objects']['pages']['search_data.json.gz']['object']] = b'bad'
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            self.run_publish()
        self.assertEqual(self.store.head, self.initial_head)
        self.assertFalse(self.remote.calls)

    def test_source_drift_is_not_retried(self):
        with self.assertRaisesRegex(ValueError, 'source revisions differ'):
            self.run_publish(revision=lambda *_: 'f' * 40)
        self.assertEqual(self.store.head, self.initial_head)
        self.assertEqual(self.elapsed, 0)

    def test_pointer_unknown_before_effect_waits_and_never_republishes(self):
        def uncertain(**kwargs):
            self.store.pointer_writes += 1
            raise TimeoutError('unknown pointer write')
        self.store.compare_and_swap = uncertain
        with self.assertRaisesRegex(subject.Blocked, 'pointer-parent-visible'):
            self.run_publish(timeout_seconds=6)
        self.assertEqual(self.store.pointer_writes, 1)
        self.assertEqual(tx.load(self.store).record['state'], 'publishing')
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch'])

    def test_pointer_readback_failure_after_effect_uses_reconcile(self):
        original = self.store.compare_and_swap
        read = self.store.read_for_update
        lost = False
        def uncertain(**kwargs):
            nonlocal lost
            original(**kwargs)
            lost = True
            raise TimeoutError('write response lost')
        def readback():
            nonlocal lost
            if lost:
                lost = False
                raise TimeoutError('first readback lost')
            return read()
        self.store.compare_and_swap = uncertain
        self.store.read_for_update = readback
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.store.pointer_writes, 1)
        self.assertTrue(any(e.get('status') == 'pointer-readback-required' for e in self.events))

    def test_success_return_with_publishing_journal_requires_pointer_reconciliation(self):
        # Exercise the boundary explicitly: the pointer adapter returns success,
        # but no journal acceptance has happened. A successful call is not state.
        from scripts.search_publication_pointer import apply_pointer
        def publish_pointer_only(store, pointer_store, **common):
            c = tx.load(store)
            subject.finalize._actual(c, **common)
            result = apply_pointer(pointer_store, desired=self.candidate, expected=self.parent,
                                   transition=c.record['transition'], expected_pointer=store.pointer)
            self.remote.stages = ['APP_STARTING']
            return result
        with patch.object(subject.finalize, 'publish', side_effect=publish_pointer_only):
            result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.store.pointer_writes, 1)
        self.assertEqual(self.elapsed, 5)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])

    def test_foreign_space_configuration_while_building_fails_without_wait(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            self.remote.stages = ['BUILDING']
            self.remote.space_generation = 'f' * 64
        self.remote.add_space_variable = write
        with self.assertRaisesRegex(ValueError, 'configuration differs while waiting'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(self.remote.calls, ['space-write'])

    def test_consumed_claim_skips_send_and_only_reconciles(self):
        original = subject.space.prepare
        def claimed(*args, **kwargs):
            c = original(*args, **kwargs)
            return tx.claim_write(self.store, c, site='space', operation='apply', operation_id=kwargs['operation_id'])
        with patch.object(subject.space, 'prepare', side_effect=claimed), \
                self.assertRaisesRegex(subject.Blocked, 'space-parent-configured'):
            self.run_publish(timeout_seconds=6)
        self.assertFalse(self.remote.calls)

    def test_programming_error_is_not_treated_as_unknown_write(self):
        def broken(*args, **kwargs):
            raise TypeError('adapter bug')
        self.remote.add_space_variable = broken
        with self.assertRaisesRegex(TypeError, 'adapter bug'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertTrue(tx.load(self.store).record['pending']['write_claim'])

    def test_fresh_final_consumer_inspection_is_required_after_finish(self):
        public_get = self.remote.public_get
        def get(url, limit):
            if tx.load(self.store).record['state'] == 'complete' and self.store.head != self.initial_head:
                self.remote.bad_consumer = True
            return public_get(url, limit)
        with self.assertRaisesRegex(ValueError, 'Space has not loaded'):
            self.run_publish(get=get)
        self.assertEqual(tx.load(self.store).record['state'], 'complete')
        self.assertEqual(self.remote.calls.count('default-write'), 1)

    def test_fresh_final_journal_audit_is_required_after_public_inspection(self):
        inspect = subject.daily.inspect_published
        def inspect_then_corrupt(**kwargs):
            result = inspect(**kwargs)
            c = tx.load(self.store)
            if c.record['state'] == 'complete' and c.record['candidate'] == self.candidate:
                self.store.receipts.clear()
            return result
        with patch.object(subject.daily, 'inspect_published', side_effect=inspect_then_corrupt), \
                self.assertRaises(ValueError):
            self.run_publish()
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])

    def test_building_wrong_sha_is_not_a_wait_condition(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            self.remote.stages = ['BUILDING']
            self.remote.runtime_sha = 'f' * 40
        self.remote.add_space_variable = write
        with self.assertRaisesRegex(ValueError, 'runtime differs'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(self.remote.calls, ['space-write'])

    def test_permanent_runtime_failure_does_not_poll(self):
        self.remote.stages = ['RUNTIME_ERROR']
        with self.assertRaisesRegex(ValueError, 'runtime is unavailable'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(self.store.head, self.initial_head)

    def test_foreign_pointer_after_dispatch_blocks_before_further_writes(self):
        self.remote.after_dispatch = lambda: setattr(self.store, 'pointer', make_pointer(self.parent, None, 'f' * 32))
        with self.assertRaisesRegex(ValueError, 'exact pointer identity changed'):
            self.run_publish()
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_source_drift_after_space_write_blocks_pages_dispatch(self):
        original = self.remote.add_space_variable
        fetch = subject.promotion_intent.preflight.fetch
        def write(*args, **kwargs):
            original(*args, **kwargs)
            fetch.get_repo_sha.return_value = 'f' * 40
        self.remote.add_space_variable = write
        with self.assertRaisesRegex(ValueError, 'candidate sources changed'):
            self.run_publish()
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertIsNone(tx.load(self.store).record['pending']['write_claim'])

    def test_read_error_is_not_retried_or_classified_as_a_write(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            self.remote.get_space_runtime = lambda *_: (_ for _ in ()).throw(TimeoutError('runtime read failed'))
        self.remote.add_space_variable = write
        with self.assertRaisesRegex(TimeoutError, 'runtime read failed'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(self.remote.calls, ['space-write'])

    def test_no_progress_success_dictionary_cannot_advance(self):
        with patch.object(subject.space, 'reconcile', return_value={'status': 'candidate-loaded', 'journal_advanced': True}), \
                self.assertRaisesRegex(ValueError, 'did not durably accept'):
            self.run_publish()
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)


if __name__ == '__main__':
    unittest.main()
