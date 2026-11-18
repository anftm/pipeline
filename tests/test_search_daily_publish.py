"""Offline integration: real orchestration, transitions, validators and adapters.

Only source-head lookup and external transports are replaced. This cannot prove
live HF/GitHub/S3 semantics, writer isolation, or production-scale timing.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

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
        pending = tx.load(self.store).record['pending']
        assert pending['write_claim'] and pending['site'] == 'space'
        assert pending['expected_revision'] == self.space_sha
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

    def test_cli_nonpublication_results_do_not_claim_completion(self):
        output = []
        with patch.dict(os.environ, {'PAGES_TOKEN': 'offline-test'}), \
                patch.object(subject.tx, 'S3TransitionStore', return_value=self.store), \
                patch.object(subject, 'S3SearchPointerStore', return_value=self.store), \
                patch.object(subject.space, 'HubBucketStore', return_value=self.remote), \
                patch('huggingface_hub.HfApi', return_value=self.remote), \
                patch.object(subject, '_emit', side_effect=output.append):
            blocked = 'search-daily-publish-blocked'
            rolled_back = 'search-daily-publish-rolled-back'
            for effect, event in ((subject.Blocked('no progress'), blocked), (None, rolled_back)):
                with self.subTest(event=event), patch.object(subject, 'publish', side_effect=effect,
                        return_value={'kind': event, 'state': 'rolled-back'}):
                    self.assertEqual(subject.main(self.cli_args()), 2)
                self.assertEqual(output[-1]['kind'], event)
        self.assertTrue(output[0]['journal_retained'])
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
        self.assertTrue(result['resume_supported'])
        self.assertEqual(result['resume_scope'], subject.RESUME_SCOPE)

    def interrupt_space_starting(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            self.remote.stages = ['RUNNING_APP_STARTING']
        self.remote.add_space_variable = write
        # Reproduce the production failure with the previous stage vocabulary.
        with patch.object(subject, 'WAIT_STAGES', subject.WAIT_STAGES - {'RUNNING_APP_STARTING'}), \
                self.assertRaisesRegex(ValueError, 'runtime is unavailable: RUNNING_APP_STARTING'):
            self.run_publish()
        current = tx.load(self.store)
        self.assertEqual(current.record['sequence'], 14)
        self.assertEqual(current.record['state'], 'applying')
        self.assertTrue(current.record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write'])
        return current

    def test_running_app_starting_waits_on_normal_path(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            self.remote.stages = ['RUNNING_APP_STARTING']
        self.remote.add_space_variable = write
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.elapsed, 5)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])

    def test_explicit_resume_after_starting_failure_observes_without_space_rewrite(self):
        interrupted = self.interrupt_space_starting()
        transaction = interrupted.record['transition']
        # Pages has no pending intent yet; a new explicit current SHA can be bound.
        self.remote.pages_sha = 'f' * 40
        result = self.run_publish(resume_transaction=transaction, pages_revision=self.remote.pages_sha)
        self.assertEqual(result['transaction'], transaction)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.elapsed, 5)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])
        self.assertEqual(self.store.pointer_writes, 1)
        self.assertTrue(any(e['kind'] == 'search-daily-publish-resume' and
                            e['operation'] == interrupted.record['pending']['operation_id'] for e in self.events))
        records = [json.loads(raw) for raw in self.store.records.values()]
        space_claims = [r['pending'] for r in records if r['transition'] == transaction and r['pending']
                        and r['pending']['site'] == 'space' and r['pending'].get('write_claim')]
        self.assertEqual(space_claims, [interrupted.record['pending']])

    def test_resume_rejects_foreign_identity_or_revision_without_mutation(self):
        interrupted = self.interrupt_space_starting()
        self.remote.stages = []
        for changes in ({'resume_transaction': 'f' * 32}, {'resume_transaction': ''},
                        {'resume_transaction': 'invalid'}, {'generation': 'f' * 64},
                        {'expected_parent': 'f' * 64}, {'space_revision': 'f' * 40},
                        {'pages_revision': 'f' * 40}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_publish(**{'resume_transaction': interrupted.record['transition'], **changes})
            self.assertEqual(tx.load(self.store), interrupted)
            self.assertEqual(self.remote.calls, ['space-write'])

    def test_resume_cannot_replace_pending_sha_even_if_live_space_matches_new_sha(self):
        interrupted = self.interrupt_space_starting()
        self.remote.stages = []
        self.remote.space_sha = self.remote.runtime_sha = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'pending operation differs from bound site revision'):
            self.run_publish(resume_transaction=interrupted.record['transition'], space_revision='f' * 40)
        self.assertEqual(tx.load(self.store), interrupted)
        self.assertEqual(self.remote.calls, ['space-write'])

    def test_resume_rejects_corrupt_candidate_before_reconciliation(self):
        interrupted = self.interrupt_space_starting()
        manifest = json.loads(self.remote.objects[f'generations/{self.candidate}/manifest.json'])
        self.remote.objects[manifest['objects']['pages']['search_data.json.gz']['object']] = b'bad'
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            self.run_publish(resume_transaction=interrupted.record['transition'])
        self.assertEqual(tx.load(self.store), interrupted)
        self.assertEqual(self.remote.calls, ['space-write'])

    def test_cli_resume_passes_exact_transaction_into_real_chain(self):
        interrupted = self.interrupt_space_starting()
        transaction = interrupted.record['transition']
        self.remote.stages = []
        self.remote.headers = {}
        real_publish = subject.publish
        output = []
        def offline_publish(**kwargs):
            self.assertEqual(kwargs['resume_transaction'], transaction)
            return real_publish(**kwargs, get=self.remote.public_get, emit=self.events.append)
        with patch.dict(os.environ, {'PAGES_TOKEN': 'offline-test'}), \
                patch.object(subject.tx, 'S3TransitionStore', return_value=self.store), \
                patch.object(subject, 'S3SearchPointerStore', return_value=self.store), \
                patch.object(subject.space, 'HubBucketStore', return_value=self.remote), \
                patch.object(subject.requests, 'Session', return_value=self.remote), \
                patch('huggingface_hub.HfApi', return_value=self.remote), \
                patch.object(subject, 'publish', side_effect=offline_publish), \
                patch.object(subject, '_emit', side_effect=output.append):
            self.assertEqual(subject.main(self.cli_args() + ['--resume-transaction', transaction]), 0)
        self.assertEqual(output[-1]['state'], 'complete')
        self.assertEqual(output[-1]['transaction'], transaction)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])

    def successor_repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE).decode().strip()
        git('init', '--quiet')
        git('config', 'user.name', 'Offline Test')
        git('config', 'user.email', 'test@example.invalid')
        path = 'tests/reader/browser/test_reader_refactor.py'
        target = root / path
        target.parent.mkdir(parents=True)
        target.write_text('assert True\n')
        (root / 'app.py').write_text('print("app")\n')
        (root / 'static/reader').mkdir(parents=True)
        (root / 'static/reader/example.mjs').write_text('export const value = 1;\n')
        git('add', '.')
        git('commit', '--quiet', '-m', 'parent')
        old = git('rev-parse', 'HEAD')
        target.write_text('assert 1 == 1\n')
        git('commit', '--quiet', '-am', 'reviewed test edit')
        new = git('rev-parse', 'HEAD')
        return root, git, old, new, path

    def test_reviewed_successor_resume_verifies_git_and_preserves_original_claim(self):
        root, git, old, new, path = self.successor_repo()
        self.remote.space_sha = self.remote.runtime_sha = old
        with patch(__name__ + '.SPACE_SHA', old), patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            interrupted = self.interrupt_space_starting()
            self.remote.stages = []
            self.remote.space_sha = self.remote.runtime_sha = new
            result = self.run_publish(resume_transaction=interrupted.record['transition'],
                                      reviewed_space_successor=new, reviewed_space_test_path=path)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])
        records = [json.loads(raw) for raw in self.store.records.values()]
        claims = [r['pending'] for r in records if r['transition'] == result['transaction'] and r['pending']
                  and r['pending']['site'] == 'space' and r['pending'].get('write_claim')]
        self.assertEqual(claims, [interrupted.record['pending']])
        witnesses = [json.loads(raw)['details']['reviewed_space_successor'] for raw in self.store.receipts.values()
                     if 'reviewed_space_successor' in json.loads(raw).get('details', {})]
        self.assertEqual(len(witnesses), 1)
        self.assertEqual(witnesses[0]['expected_revision'], old)
        self.assertEqual(witnesses[0]['successor'], new)
        self.assertEqual(witnesses[0]['reviewed_test_path'], path)

    def test_successor_verifier_rejects_unreviewed_paths_modes_and_non_direct_parent(self):
        root, git, old, new, path = self.successor_repo()
        with patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            for bad in ('tests/*.py', 'app.py', 'tests/../app.py', ''):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    subject.space.verify_reviewed_successor(expected_revision=old, successor=new, test_path=bad)
            (root / 'app.py').write_text('print("changed")\n')
            git('commit', '--quiet', '-am', 'runtime change')
            runtime_change = git('rev-parse', 'HEAD')
            with self.assertRaisesRegex(ValueError, 'outside the exact reviewed allowlist'):
                subject.space.verify_reviewed_successor(expected_revision=old, successor=runtime_change, test_path=path)
            with self.assertRaisesRegex(ValueError, 'outside the exact reviewed allowlist'):
                subject.space.verify_reviewed_successor(expected_revision=new, successor=runtime_change, test_path=path)
            git('reset', '--hard', new)
            git('update-index', '--chmod=+x', path)
            git('commit', '--quiet', '-m', 'mode change')
            mode_change = git('rev-parse', 'HEAD')
            with self.assertRaisesRegex(ValueError, 'unchanged mode'):
                subject.space.verify_reviewed_successor(expected_revision=new, successor=mode_change, test_path=path)

    def test_successor_verifier_rejects_merge_even_with_only_reviewed_tree_change(self):
        root, git, old, new, path = self.successor_repo()
        merge = git('commit-tree', git('rev-parse', new + '^{tree}'), '-p', old, '-p', new, '-m', 'merge')
        with patch.object(subject.space, 'SPACE_GIT_URL', str(root)), \
                self.assertRaisesRegex(ValueError, 'single-parent chain'):
            subject.space.verify_reviewed_successor(expected_revision=old, successor=merge, test_path=path)

    def three_path_successor_repo(self):
        root, git, old, first, path = self.successor_repo()
        reader = 'static/reader/example.mjs'
        added = 'tests/reader/browser/test_added.py'
        (root / reader).write_text('export const value = 2;\n')
        (root / added).write_text('assert True\n')
        git('add', '.')
        git('commit', '--quiet', '-m', 'reviewed Reader edit and new test')
        new = git('rev-parse', 'HEAD')
        return root, git, old, first, new, [path, reader, added]

    def test_two_commit_three_path_proof_and_complete_resume_write_once(self):
        root, git, old, first, new, paths = self.three_path_successor_repo()
        self.remote.space_sha = self.remote.runtime_sha = old
        with patch(__name__ + '.SPACE_SHA', old), patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            proof = subject.space.verify_reviewed_successor(expected_revision=old, successor=new, reviewed_paths=paths)
            self.assertEqual(proof['chain'], [old, first, new])
            added = next(f for f in proof['files'] if f['path'] == paths[2])
            self.assertIsNone(added['before_blob'])
            self.assertIsNone(added['before_mode'])
            self.assertEqual(added['after_mode'], '100644')
            interrupted = self.interrupt_space_starting()
            self.remote.stages = []
            self.remote.space_sha = self.remote.runtime_sha = new
            real_publish = subject.publish
            output = []
            self.remote.headers = {}
            def offline_publish(**kwargs):
                self.assertEqual(kwargs['reviewed_space_path'], paths)
                return real_publish(**kwargs, get=self.remote.public_get, emit=self.events.append)
            args = self.cli_args() + ['--resume-transaction', interrupted.record['transition'],
                                      '--reviewed-space-successor', new]
            for path in paths:
                args += ['--reviewed-space-path', path]
            with patch.dict(os.environ, {'PAGES_TOKEN': 'offline-test'}), \
                    patch.object(subject.tx, 'S3TransitionStore', return_value=self.store), \
                    patch.object(subject, 'S3SearchPointerStore', return_value=self.store), \
                    patch.object(subject.space, 'HubBucketStore', return_value=self.remote), \
                    patch.object(subject.requests, 'Session', return_value=self.remote), \
                    patch('huggingface_hub.HfApi', return_value=self.remote), \
                    patch.object(subject, 'publish', side_effect=offline_publish), \
                    patch.object(subject, '_emit', side_effect=output.append):
                self.assertEqual(subject.main(args), 0)
            result = output[-1]
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])
        self.assertEqual(self.store.pointer_writes, 1)
        witnesses = [json.loads(raw)['details']['reviewed_space_successor'] for raw in self.store.receipts.values()
                     if 'reviewed_space_successor' in json.loads(raw).get('details', {})]
        self.assertEqual(witnesses, [proof])
        self.assertEqual(interrupted.record['pending']['expected_revision'], old)

    def test_reviewed_allowlist_rejects_missing_extra_and_forbidden_paths(self):
        root, git, old, first, new, paths = self.three_path_successor_repo()
        with patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            for allow in (paths[:-1], paths + ['tests/not_changed.py'], paths + [paths[0]]):
                with self.subTest(allow=allow), self.assertRaises(ValueError):
                    subject.space.verify_reviewed_successor(expected_revision=old, successor=new, reviewed_paths=allow)
            for forbidden in ('backend/app.py', 'scripts/test.py', 'Dockerfile', 'requirements.txt',
                              'static/app.mjs', 'config.py', 'tests/*.py', 'static/reader/../app.mjs'):
                with self.subTest(path=forbidden), self.assertRaises(ValueError):
                    subject.space.verify_reviewed_successor(expected_revision=old, successor=new,
                                                           reviewed_paths=paths + [forbidden])
            (root / 'backend').mkdir()
            (root / 'backend/app.py').write_text('changed = True\n')
            git('add', '.')
            git('commit', '--quiet', '-m', 'backend change')
            backend = git('rev-parse', 'HEAD')
            with self.assertRaisesRegex(ValueError, 'outside the exact reviewed allowlist'):
                subject.space.verify_reviewed_successor(expected_revision=old, successor=backend, reviewed_paths=paths)

    def test_reviewed_chain_rejects_unrelated_ancestor_and_more_than_31_successors(self):
        root, git, old, new, path = self.successor_repo()
        with patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            with self.assertRaisesRegex(ValueError, 'single-parent chain'):
                subject.space.verify_reviewed_successor(expected_revision='f' * 40, successor=new, test_path=path)
            for i in range(31):
                (root / path).write_text('assert ' + str(i + 2) + '\n')
                git('commit', '--quiet', '-am', 'reviewed test change')
            latest = git('rev-parse', 'HEAD')
            with self.assertRaisesRegex(ValueError, 'within 31 successors'):
                subject.space.verify_reviewed_successor(expected_revision=old, successor=latest, test_path=path)
            allowed = subject.space.verify_reviewed_successor(expected_revision=new, successor=latest, test_path=path)
            self.assertEqual(len(allowed['chain']), 32)

    def test_added_test_must_be_regular_100644_and_new_reader_file_is_rejected(self):
        root, git, old, first, new, paths = self.three_path_successor_repo()
        with patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            git('update-index', '--chmod=+x', paths[2])
            git('commit', '--quiet', '-m', 'executable added test')
            with self.assertRaisesRegex(ValueError, 'regular 100644'):
                subject.space.verify_reviewed_successor(expected_revision=old, successor=git('rev-parse', 'HEAD'),
                                                       reviewed_paths=paths)
            git('reset', '--hard', new)
            extra = 'static/reader/added.mjs'
            (root / extra).write_text('export const added = 1;\n')
            git('add', '.')
            git('commit', '--quiet', '-m', 'new reader file')
            with self.assertRaisesRegex(ValueError, 'only regular 100644 tests may be added'):
                subject.space.verify_reviewed_successor(expected_revision=old, successor=git('rev-parse', 'HEAD'),
                                                       reviewed_paths=paths + [extra])

    def test_reviewed_successor_restarts_during_consumer_readback_and_reobserves(self):
        root, git, old, new, path = self.successor_repo()
        self.remote.space_sha = self.remote.runtime_sha = old
        with patch(__name__ + '.SPACE_SHA', old), patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            interrupted = self.interrupt_space_starting()
            self.remote.stages = []
            self.remote.space_sha = self.remote.runtime_sha = new
            get = self.remote.public_get
            reads = []
            def readback(url, limit):
                result = get(url, limit)
                if url.endswith('/api/search-data-generation') and tx.load(self.store) == interrupted:
                    reads.append(result)
                    if len(reads) == 1:
                        self.remote.stages = ['BUILDING']
                        self.remote.runtime_sha = None  # no SHA exposed while building
                return result
            def sleep(seconds):
                self.assertEqual(tx.load(self.store), interrupted)
                self.sleep(seconds)
                self.remote.runtime_sha = new
            result = self.run_publish(resume_transaction=interrupted.record['transition'],
                                      reviewed_space_successor=new, reviewed_space_test_path=path,
                                      get=readback, sleep=sleep)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.elapsed, 5)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])

    def test_reviewed_successor_drift_during_consumer_observation_keeps_claim_pending(self):
        root, git, old, new, path = self.successor_repo()
        self.remote.space_sha = self.remote.runtime_sha = old
        with patch(__name__ + '.SPACE_SHA', old), patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            interrupted = self.interrupt_space_starting()
            self.remote.stages = []
            self.remote.space_sha = self.remote.runtime_sha = new
            original = self.remote.public_get
            def get(url, limit):
                result = original(url, limit)
                if url.endswith('/api/search-data-generation'):
                    self.remote.space_sha = self.remote.runtime_sha = 'f' * 40
                return result
            with self.assertRaisesRegex(ValueError, 'Space repository differs'):
                self.run_publish(resume_transaction=interrupted.record['transition'],
                                 reviewed_space_successor=new, reviewed_space_test_path=path, get=get)
        self.assertEqual(tx.load(self.store), interrupted)
        self.assertEqual(self.remote.calls, ['space-write'])

    def test_reviewed_successor_requires_resume_and_current_exact_runtime(self):
        root, git, old, new, path = self.successor_repo()
        with self.assertRaisesRegex(ValueError, 'require explicit resume'):
            self.run_publish(reviewed_space_successor=new, reviewed_space_test_path=path)
        self.remote.space_sha = self.remote.runtime_sha = old
        with patch(__name__ + '.SPACE_SHA', old), patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            interrupted = self.interrupt_space_starting()
            with self.assertRaisesRegex(ValueError, 'repository differs'):
                self.run_publish(resume_transaction=interrupted.record['transition'],
                                 reviewed_space_successor=new, reviewed_space_test_path=path)
        self.assertEqual(tx.load(self.store), interrupted)
        self.assertEqual(self.remote.calls, ['space-write'])

    def test_resume_rejects_source_drift_before_reconciliation(self):
        interrupted = self.interrupt_space_starting()
        self.remote.stages = []
        with patch.object(subject.promotion_intent.preflight.fetch, 'get_repo_sha', return_value='f' * 40), \
                self.assertRaisesRegex(ValueError, 'candidate sources changed'):
            self.run_publish(resume_transaction=interrupted.record['transition'])
        self.assertEqual(tx.load(self.store), interrupted)
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_resume_unknown_parent_still_blocks_without_replay(self):
        interrupted = self.interrupt_space_starting()
        self.remote.stages = []
        self.remote.space_generation = self.remote.loaded_space = self.parent
        with self.assertRaisesRegex(subject.Blocked, 'space-parent-configured'):
            self.run_publish(resume_transaction=interrupted.record['transition'], timeout_seconds=6)
        self.assertEqual(tx.load(self.store), interrupted)
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_resume_rejects_other_journal_states_and_unclaimed_space(self):
        # Even an exact completed transaction is outside this narrow recovery.
        terminal = tx.load(self.store)
        with self.assertRaisesRegex(ValueError, 'only a claimed Space apply'):
            self.run_publish(resume_transaction=terminal.record['transition'], generation=self.parent,
                             expected_parent=terminal.record['parent_pointer']['desired'])
        original = subject.space.prepare
        class Interrupted(Exception):
            pass
        def prepare(*args, **kwargs):
            original(*args, **kwargs)
            raise Interrupted()
        with patch.object(subject.space, 'prepare', side_effect=prepare), self.assertRaises(Interrupted):
            self.run_publish()
        unclaimed = tx.load(self.store)
        self.assertIsNone(unclaimed.record['pending']['write_claim'])
        with self.assertRaisesRegex(ValueError, 'only a claimed Space apply'):
            self.run_publish(resume_transaction=unclaimed.record['transition'])
        self.assertEqual(tx.load(self.store), unclaimed)
        self.assertFalse(self.remote.calls)

    def test_all_lost_write_responses_are_observed_without_replay(self):
        self.remote.loss = {'space', 'pages', 'default'}
        self.store.lose_pointer_response = True
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])
        self.assertEqual(self.store.pointer_writes, 1)
        self.assertEqual(sum(e['kind'] == 'search-daily-publish-unknown' for e in self.events), 3)

    def test_space_httpx_read_timeout_after_effect_reconciles_without_replay(self):
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            raise httpx.ReadTimeout('Space response lost')
        self.remote.add_space_variable = write
        result = self.run_publish()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.remote.space_generation, self.candidate)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch', 'default-write'])
        self.assertEqual(self.store.pointer_writes, 1)
        unknown = [e for e in self.events if e['kind'] == 'search-daily-publish-unknown']
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]['error_type'], 'ReadTimeout')

    def test_space_httpx_read_timeout_without_effect_blocks_without_replay(self):
        self.remote.no_effect = {'space'}
        original = self.remote.add_space_variable
        def write(*args, **kwargs):
            original(*args, **kwargs)
            raise httpx.ReadTimeout('Space write outcome unknown')
        self.remote.add_space_variable = write
        with self.assertRaisesRegex(subject.Blocked, 'space-parent-configured'):
            self.run_publish(timeout_seconds=6)
        current = tx.load(self.store)
        self.assertTrue(current.record['pending']['write_claim'])
        self.assertEqual(current.record['observed']['space']['generation'], self.parent)
        self.assertEqual(self.remote.space_generation, self.parent)
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)
        self.assertEqual(self.elapsed, 6)
        unknown = [e for e in self.events if e['kind'] == 'search-daily-publish-unknown']
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]['error_type'], 'ReadTimeout')

    def test_httpx_read_timeout_during_observation_is_not_retried(self):
        def read(*args, **kwargs):
            raise httpx.ReadTimeout('Space runtime read failed')
        self.remote.get_space_runtime = read
        with self.assertRaisesRegex(httpx.ReadTimeout, 'Space runtime read failed'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertEqual(self.store.head, self.initial_head)
        self.assertFalse(self.remote.calls)
        self.assertFalse(self.events)

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
        claimed = tx.load(self.store)
        self.assertTrue(claimed.record['pending']['write_claim'])
        with self.assertRaisesRegex(ValueError, 'only a claimed Space apply before Pages'):
            self.run_publish(resume_transaction=claimed.record['transition'], revision=lambda *_: 'f' * 40)
        self.assertEqual(tx.load(self.store), claimed)
        self.assertEqual(self.remote.calls, ['space-write', 'pages-dispatch'])

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

    def change_source_after_space_apply(self, sha='f' * 40, *, error=None):
        original = self.remote.add_space_variable
        fetch = subject.promotion_intent.preflight.fetch
        def write(*args, **kwargs):
            original(*args, **kwargs)
            if kwargs['value'] == self.candidate:
                fetch.get_repo_sha.return_value = sha
                fetch.get_repo_sha.side_effect = error
        self.remote.add_space_variable = write

    def interrupt_before_pages_claim(self):
        # Interrupt after real Pages preparation; no dispatch or synthetic state.
        with patch.object(subject.pages, 'apply_once', side_effect=TimeoutError('interrupted')), \
                self.assertRaisesRegex(TimeoutError, 'interrupted'):
            self.run_publish()
        active = tx.load(self.store)
        self.assertEqual(active.record['pending']['site'], 'pages')
        self.assertIsNone(active.record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write'])
        return active

    def assert_source_rollback(self, result, parent_pointer):
        active = tx.load(self.store)
        self.assertEqual(result['kind'], 'search-daily-publish-rolled-back')
        self.assertEqual(result['state'], 'rolled-back')
        self.assertEqual(result['generation'], self.parent)
        self.assertEqual(result['candidate'], self.candidate)
        self.assertEqual(result['transaction'], active.record['transition'])
        self.assertEqual(active.record['state'], 'rolled-back')
        self.assertIsNone(active.record['pending'])
        self.assertEqual(self.remote.calls, ['space-write', 'space-write'])
        self.assertIsNone(self.remote.run)
        self.assertEqual(self.store.pointer, parent_pointer)
        self.assertEqual(self.store.pointer_writes, 0)
        self.assertEqual((self.remote.space_generation, self.remote.loaded_space,
                          self.remote.pages_generation, self.remote.default), (self.parent,) * 4)
        for field in ('observed', 'defaults'):
            self.assertEqual({item['generation'] for item in active.record[field].values()}, {self.parent})
        self.assertEqual(subject.daily_journal.inspect(self.store)['state'], 'rolled-back')
        records = sorted((r for raw in self.store.records.values()
                          if (r := json.loads(raw))['transition'] == result['transaction']),
                         key=lambda r: r['sequence'])
        pending = [r['pending'] for r in records if r['pending']]
        self.assertEqual([(p['site'], p['operation'], bool(p['write_claim'])) for p in pending],
                         [('space', 'apply', False), ('space', 'apply', True), ('pages', 'apply', False),
                          ('space', 'rollback', False), ('space', 'rollback', True)])
        self.assertEqual([(r['state'], r['pending'] is not None) for r in records[-5:]],
                         [('rolling-back', False), ('rolling-back', True), ('rolling-back', True),
                          ('rolling-back', False), ('rolled-back', False)])

    def test_source_drift_after_space_write_abandons_pages_and_finishes_rollback(self):
        parent_pointer = self.store.pointer
        self.change_source_after_space_apply()
        with patch.object(subject.pages, 'abandon_unclaimed_apply', wraps=subject.pages.abandon_unclaimed_apply) as abandon, \
                patch.object(subject.finalize, 'publish', side_effect=AssertionError('must not publish')):
            self.assert_source_rollback(self.run_publish(), parent_pointer)
        abandon.assert_called_once()

    def test_caller_source_lookup_drift_after_space_write_also_rolls_back(self):
        parent_pointer = self.store.pointer
        def revision(*_):
            return 'f' * 40 if self.remote.space_generation == self.candidate else 'a' * 40
        self.assert_source_rollback(self.run_publish(revision=revision), parent_pointer)

    def test_same_process_rollback_after_reviewed_successor_resume_uses_bound_successor(self):
        root, git, old, new, path = self.successor_repo()
        parent_pointer = self.store.pointer
        self.remote.space_sha = self.remote.runtime_sha = old
        prepare = subject.pages.prepare
        def stale_after_prepare(*args, **kwargs):
            intent = prepare(*args, **kwargs)
            subject.promotion_intent.preflight.fetch.get_repo_sha.return_value = 'f' * 40
            return intent
        with patch(__name__ + '.SPACE_SHA', old), patch.object(subject.space, 'SPACE_GIT_URL', str(root)):
            interrupted = self.interrupt_space_starting()
            self.remote.stages = []
            self.remote.space_sha = self.remote.runtime_sha = new
            with patch.object(subject.pages, 'prepare', side_effect=stale_after_prepare):
                result = self.run_publish(resume_transaction=interrupted.record['transition'],
                                          reviewed_space_successor=new, reviewed_space_test_path=path)
        self.assert_source_rollback(result, parent_pointer)
        claims = {r['pending']['operation']: r['pending'] for raw in self.store.records.values()
                  if (r := json.loads(raw))['transition'] == result['transaction']
                  and r['pending'] and r['pending'].get('write_claim')}
        self.assertEqual(claims['apply'], interrupted.record['pending'])
        self.assertEqual(claims['rollback']['expected_revision'], new)

    def test_explicit_resume_rejects_unclaimed_pages_even_with_stale_sources_and_live_sha(self):
        intent = self.interrupt_before_pages_claim()
        for live_sha in (SPACE_SHA, 'f' * 40):
            self.remote.space_sha = self.remote.runtime_sha = live_sha
            with self.subTest(live_sha=live_sha), \
                    patch.object(subject.pages, 'abandon_unclaimed_apply') as abandon, \
                    self.assertRaisesRegex(ValueError, 'only a claimed Space apply before Pages'):
                self.run_publish(resume_transaction=intent.record['transition'], space_revision=live_sha,
                                 revision=lambda *_: 'f' * 40)
            abandon.assert_not_called()
            self.assertEqual(tx.load(self.store), intent)
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_malformed_or_unavailable_live_sha_is_not_source_drift(self):
        fetch = subject.promotion_intent.preflight.fetch
        revisions = {'VoiceOfML/Test': 'a' * 40}
        digest = hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()
        with patch.object(fetch, 'REPOS', list(revisions)):
            for sha in ('', None, 'F' * 40, 'f' * 39, 'f' * 40 + '\n', 'g' * 40, '0' * 40):
                with self.subTest(sha=sha), \
                        self.assertRaisesRegex(ValueError, 'live search source revision is unavailable or malformed') as error:
                    subject.space._verify_candidate_sources({'source_revisions': revisions}, digest,
                                                            revision=lambda *_: sha)
                self.assertNotIsInstance(error.exception, subject.space.CandidateSourcesChanged)

    def test_same_process_rollback_rejects_space_sha_drift_before_abandonment(self):
        def revision(*_):
            if self.remote.space_generation == self.candidate:
                self.remote.space_sha = self.remote.runtime_sha = 'f' * 40
                return 'f' * 40
            return 'a' * 40
        with patch.object(subject.pages, 'abandon_unclaimed_apply') as abandon, \
                self.assertRaisesRegex(ValueError, 'Space repository differs'):
            self.run_publish(revision=revision)
        abandon.assert_not_called()
        active = tx.load(self.store)
        self.assertEqual(active.record['pending']['site'], 'pages')
        self.assertIsNone(active.record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_same_process_rejects_space_sha_race_inside_pages_abandonment(self):
        self.change_source_after_space_apply()
        abandon = subject.pages.abandon_unclaimed_apply
        def race(store, **kwargs):
            self.remote.space_sha = self.remote.runtime_sha = 'f' * 40
            return abandon(store, **kwargs)
        with patch.object(subject.pages, 'abandon_unclaimed_apply', side_effect=race), \
                self.assertRaisesRegex(ValueError, 'Space repository or running revision differs'):
            self.run_publish()
        active = tx.load(self.store)
        self.assertEqual(active.record['pending']['site'], 'pages')
        self.assertIsNone(active.record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_empty_live_sha_from_pages_preclaim_reads_does_not_abandon(self):
        self.change_source_after_space_apply('')
        with self.assertRaisesRegex(ValueError, 'live search source revision is unavailable or malformed'):
            self.run_publish()
        active = tx.load(self.store)
        self.assertEqual(active.record['pending']['site'], 'pages')
        self.assertIsNone(active.record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertEqual(self.store.pointer_writes, 0)

    def test_source_read_failure_in_pages_preclaim_does_not_abandon(self):
        self.change_source_after_space_apply(error=httpx.ReadTimeout('upstream source timed out'))
        with self.assertRaisesRegex(httpx.ReadTimeout, 'upstream source timed out'):
            self.run_publish()
        active = tx.load(self.store)
        self.assertEqual(active.record['pending']['site'], 'pages')
        self.assertIsNone(active.record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertFalse(self.store.pointer_writes)
        self.assertEqual(self.elapsed, 0)

    def test_changed_repo_cannot_mask_other_unknown_repo(self):
        fetch = subject.promotion_intent.preflight.fetch
        revisions = {'VoiceOfML/Test': 'a' * 40, 'VoiceOfML/Other': 'b' * 40}
        digest = hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()
        with patch.object(fetch, 'REPOS', list(revisions)):
            with self.assertRaisesRegex(ValueError, 'unavailable or malformed') as error:
                subject.space._verify_candidate_sources({'source_revisions': revisions}, digest,
                    revision=lambda repo, _token: 'f' * 40 if repo == 'VoiceOfML/Test' else '')
            self.assertNotIsInstance(error.exception, subject.space.CandidateSourcesChanged)
            with self.assertRaisesRegex(ValueError, 'durable intent'):
                subject.space._verify_candidate_sources({'source_revisions': revisions}, 'f' * 64,
                    revision=lambda *_: self.fail('invalid candidate must never query live heads'))

    def test_pages_run_appearing_before_abandonment_prevents_rollback(self):
        self.change_source_after_space_apply()
        abandon = subject.pages.abandon_unclaimed_apply
        def run_appeared(store, **kwargs):
            p = tx.load(store).record['pending']
            self.remote.run = {'id': 1, 'run_attempt': 1, 'event': 'workflow_dispatch', 'head_branch': 'main',
                               'head_sha': PAGES_SHA, 'path': '.github/workflows/static.yml',
                               'created_at': p['created_at'], 'display_title': subject.pages.deploy.run_name(p['operation_id']),
                               'status': 'completed', 'conclusion': 'success'}
            return abandon(store, **kwargs)
        with patch.object(subject.pages, 'abandon_unclaimed_apply', side_effect=run_appeared), \
                self.assertRaisesRegex(ValueError, 'Pages run exists'):
            self.run_publish()
        self.assertEqual(tx.load(self.store).record['pending']['site'], 'pages')
        self.assertEqual(self.remote.calls, ['space-write'])
        self.assertFalse(self.store.pointer_writes)

    def test_lost_rollback_response_observes_parent_after_build_without_replay(self):
        parent_pointer = self.store.pointer
        self.change_source_after_space_apply()
        write = self.remote.add_space_variable
        def lose_rollback(*args, **kwargs):
            write(*args, **kwargs)
            if kwargs['value'] == self.parent:
                raise httpx.ReadTimeout('lost rollback response after effect')
        self.remote.add_space_variable = lose_rollback
        reconcile = subject.space.reconcile
        seen = []
        def race(store, **kwargs):
            if tx.load(store).record['pending']['operation'] == 'rollback' and not seen:
                # identities() just saw RUNNING. Reconciliation must see the
                # restart as wait-only, even after a lost write response.
                self.remote.stages = ['RUNNING_APP_STARTING']
                result = reconcile(store, **kwargs)
                seen.append(result)
                self.assertTrue(tx.load(store).record['pending']['write_claim'])
                return result
            return reconcile(store, **kwargs)
        with patch.object(subject.space, 'reconcile', side_effect=race):
            self.assert_source_rollback(self.run_publish(), parent_pointer)
        self.assertEqual([(r['status'], r['journal_advanced']) for r in seen], [('building', False)])
        self.assertEqual(self.elapsed, 5)
        self.assertEqual([e['error_type'] for e in self.events if e['kind'] == 'search-daily-publish-unknown'],
                         ['ReadTimeout'])

    def test_wrong_sha_in_transient_runtime_race_is_not_polled(self):
        interrupted = self.interrupt_space_starting()
        reconcile = subject.space.reconcile
        for field, error in (('space_sha', 'repository'), ('runtime_sha', 'runtime')):
            with self.subTest(field=field):
                self.remote.stages = []
                def race(store, **kwargs):
                    self.remote.stages = ['RUNNING_BUILDING']
                    setattr(self.remote, field, 'f' * 40)
                    return reconcile(store, **kwargs)
                with patch.object(subject.space, 'reconcile', side_effect=race), \
                        self.assertRaisesRegex(ValueError, f'Space {error} differs'):
                    self.run_publish(resume_transaction=interrupted.record['transition'])
                self.assertEqual(tx.load(self.store), interrupted)
                self.assertEqual(self.elapsed, 0)
                self.assertEqual(self.remote.calls, ['space-write'])
                self.assertEqual(self.store.pointer_writes, 0)
                setattr(self.remote, field, SPACE_SHA)

    def test_unknown_rollback_without_effect_blocks_without_retry_or_resume(self):
        self.change_source_after_space_apply()
        write = self.remote.add_space_variable
        def lose_rollback(*args, **kwargs):
            if kwargs['value'] == self.parent:
                self.remote.no_effect = self.remote.loss = {'space'}
            write(*args, **kwargs)
        self.remote.add_space_variable = lose_rollback
        with self.assertRaisesRegex(subject.Blocked, 'space-candidate-configured'):
            self.run_publish(timeout_seconds=6)
        active = tx.load(self.store)
        self.assertEqual(active.record['state'], 'rolling-back')
        self.assertEqual(active.record['pending']['operation'], 'rollback')
        self.assertTrue(active.record['pending']['write_claim'])
        with self.assertRaisesRegex(ValueError, 'resume supports only'):
            self.run_publish(resume_transaction=active.record['transition'])
        self.assertEqual(tx.load(self.store), active)
        self.assertEqual(self.remote.calls, ['space-write', 'space-write'])
        self.assertFalse(self.store.pointer_writes)

    def test_rollback_parent_config_without_parent_consumer_cannot_finish(self):
        self.change_source_after_space_apply()
        write = self.remote.add_space_variable
        def stale_consumer(*args, **kwargs):
            write(*args, **kwargs)
            if kwargs['value'] == self.parent:
                self.remote.loaded_space = self.candidate
        self.remote.add_space_variable = stale_consumer
        with self.assertRaisesRegex(ValueError, 'Space has not verified'):
            self.run_publish()
        active = tx.load(self.store)
        self.assertEqual(active.record['state'], 'rolling-back')
        self.assertTrue(active.record['pending']['write_claim'])
        self.assertEqual(active.record['observed']['space']['generation'], self.candidate)
        self.assertEqual(self.remote.calls, ['space-write', 'space-write'])
        self.assertFalse(self.store.pointer_writes)

    def test_rollback_wait_rejects_regressed_configuration_timestamp(self):
        self.change_source_after_space_apply()
        write = self.remote.add_space_variable
        def regress(*args, **kwargs):
            if kwargs['value'] == self.parent:
                self.remote.no_effect = self.remote.loss = {'space'}
                self.remote.get_space_variables = lambda _: {
                    subject.finalize.VARIABLE: SimpleNamespace(value=self.candidate, updated_at='2026-11-14T00:00:00Z')}
            write(*args, **kwargs)
        self.remote.add_space_variable = regress
        with self.assertRaisesRegex(ValueError, 'timestamp moved backwards'):
            self.run_publish()
        self.assertEqual(self.elapsed, 0)
        self.assertTrue(tx.load(self.store).record['pending']['write_claim'])
        self.assertEqual(self.remote.calls, ['space-write', 'space-write'])
        self.assertFalse(self.store.pointer_writes)

    def test_finish_rollback_requires_pages_default_to_still_be_parent(self):
        self.change_source_after_space_apply()
        write = self.remote.add_space_variable
        def foreign_default(*args, **kwargs):
            write(*args, **kwargs)
            if kwargs['value'] == self.parent:
                self.remote.default = self.candidate
        self.remote.add_space_variable = foreign_default
        with self.assertRaisesRegex(ValueError, 'configured search default does not match expected generation'):
            self.run_publish()
        active = tx.load(self.store)
        self.assertEqual(active.record['state'], 'rolling-back')
        self.assertIsNone(active.record['pending'])
        self.assertEqual(active.record['observed']['space']['generation'], self.parent)
        self.assertEqual(self.remote.calls, ['space-write', 'space-write'])
        self.assertFalse(self.store.pointer_writes)

    def test_missing_bootstrap_journal_is_not_created(self):
        self.store.head = self.store.etag = None
        with self.assertRaisesRegex(ValueError, 'journal is absent'):
            self.run_publish()
        self.assertIsNone(tx.load(self.store))
        self.assertFalse(self.remote.calls)
        self.assertFalse(self.store.pointer_writes)

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
