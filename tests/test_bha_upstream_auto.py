import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import bha_upstream_auto as auto
from scripts import bha_build_plan as planning


def sha(letter):
    return letter * 40


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.writes = []
        self.lose = None

    def get(self, key):
        raw = self.data.get(key)
        return raw, '"' + auto.digest(raw) + '"' if raw is not None else None

    def put(self, key, raw, etag=None):
        self.writes.append(key)
        if self.get(key)[1] != etag:
            raise RuntimeError('CAS conflict')
        self.data[key] = raw
        if self.lose == key or self.lose == 'all':
            raise TimeoutError('lost response')


def fixtures(archive=0):
    mirror = {b: sha('a') for b in auto.builder.INPUT_BRANCHES}
    mirror['config'] = sha('c')  # preserved correction
    mirror['parsed'] = sha('d')
    upstream = {**mirror, 'main': sha('b'), 'config': sha('e')}
    snapshot = {'archive': archive, 'mirror_repository_id': 2, 'upstream_repository_id': 1,
                'mirror_branches': mirror, 'upstream_branches': upstream,
                'helper_revision': sha('f'), 'parsed_tree': sha('1')}
    recorded = auto.builder.source_snapshot(mirror, sha('f'))
    trees = {str(i): sha('1') for i in range(32)}
    def plan(snap):
        body = planning._body(archive, snap, {str(archive): recorded}, trees)
        return {**body, 'plan_sha256': auto.digest(auto.search_snapshot.canonical(body))}
    initial = plan(snapshot)
    normalized = copy.deepcopy(snapshot)
    normalized['mirror_branches']['main'] = sha('b')
    final = plan(normalized)
    row = {'archive': archive, 'mirror_repository_id': 2, 'upstream_repository_id': 1,
           'mirror_inputs': recorded, 'upstream_inputs': auto.builder.source_snapshot(upstream, sha('f')),
           'input_changed': False}
    def branch(number, name):
        return {'archive': number, 'branch': name, 'mirror_repository_id': 2, 'upstream_repository_id': 1,
                'mirror': mirror[name], 'upstream': upstream[name],
                'mirror_relation': 'behind' if name == 'main' else 'ahead',
                'can_fast_forward': name == 'main'}
    candidate = {'archive': archive, 'commit': sha('2'), 'tree': sha('3')}
    value = {'status': 'candidate-ready', 'plan': final, 'candidate': candidate,
             'bundle': {'filename': 'parsed.bundle', 'sha256': '4' * 64, 'bytes': 12}}
    return row, branch, initial, final, value


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.row, self.branch, self.initial, self.final, self.value = fixtures()
        self.ops = Mock()
        self.ops.monitor.return_value = {'archives': [self.row]}
        self.ops.branch.side_effect = self.branch
        self.ops.plan.side_effect = [self.initial, self.final]
        self.ops.build.return_value = self.value
        self.proof = auto.published_receipt(self.value)
        self.ops.publish.return_value = self.proof
        self.ops.snapshot.return_value = auto.published_snapshot(self.proof)
        self.ops.deliver.return_value = {'status': 'verified', 'ok': True}
        self.handoff = {'kind': 'bha-parsed-baseline-candidate', 'archive': 0, 'baseline_published': False}
        self.ops.baseline.return_value = self.handoff
        self.ops.adopted.return_value = False
        def sync(plan):
            self.ops.branch.side_effect = lambda n, b: {**self.branch(n, b), 'mirror': sha('b')} if b == 'main' else self.branch(n, b)
        self.ops.sync.side_effect = sync

    def run_auto(self, action='auto'):
        return auto.run(action=action, receipts=auto.Receipts(self.store), ops=self.ops,
                        provenance={'repository': 'anftm/pipeline', 'sha': sha('9'), 'run_id': '123'})

    def test_nochange_preserves_corrections_without_any_writes(self):
        rows = []
        for n in range(32):
            row = copy.deepcopy(self.row)
            row['archive'] = n
            row['upstream_inputs'] = dict(row['mirror_inputs'])
            if n in {0, 2, 4, 25}:
                row['upstream_inputs']['config'] = sha('e')
            rows.append(row)
        self.ops.monitor.return_value = {'archives': rows}
        result = self.run_auto()
        self.assertEqual(result['status'], 'no-change')
        self.assertEqual([r['archive'] for r in result['preserved_corrections']], [0, 2, 4, 25])
        self.assertEqual(self.store.writes, [])
        self.ops.build.assert_not_called()
        self.ops.sync.assert_not_called()
        self.ops.publish.assert_not_called()
        self.ops.deliver.assert_not_called()

    def test_diverged_fork_is_blocked_even_without_mirror_input_change(self):
        self.ops.branch.side_effect = lambda n, b: {**self.branch(n, b), 'mirror_relation': 'diverged', 'can_fast_forward': False}
        result = self.run_auto()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.store.writes, [])
        self.ops.sync.assert_not_called()

    def test_inspect_is_readonly_even_for_eligible_change(self):
        self.assertEqual(self.run_auto('inspect')['status'], 'eligible')
        self.assertEqual(self.store.writes, [])
        self.ops.build.assert_not_called()

    def test_full_path_uses_durable_intent_and_reuses_exact_handoff(self):
        original_sync = self.ops.sync.side_effect
        def sync(plan):
            self.assertEqual(auto.Receipts(self.store).state['stage'], 'sync-intent')
            original_sync(plan)
        self.ops.sync.side_effect = sync
        def publish(value, checkpoint):
            receipts = auto.Receipts(self.store)
            self.assertEqual(receipts.state['stage'], 'publish-intent')
            self.assertEqual(receipts.read(receipts.state['candidate']), value)
            return self.proof
        self.ops.publish.side_effect = publish
        result = self.run_auto()
        self.assertEqual(result['status'], 'baseline-deferred')
        before = len(self.store.writes)
        self.ops.reset_mock()
        self.assertTrue(self.run_auto()['reused'])
        self.assertEqual(len(self.store.writes), before)
        self.ops.monitor.assert_not_called()
        self.ops.build.assert_not_called()
        self.ops.deliver.assert_not_called()
        self.ops.export.assert_any_call('handoff.json', self.handoff)
        self.ops.export.assert_any_call('publication.json', self.proof)

    def test_lost_sync_response_recovers_target_without_second_push(self):
        sync = self.ops.sync.side_effect
        def lost(plan):
            sync(plan)
            raise TimeoutError('unknown')
        self.ops.sync.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.run_auto()
        self.assertEqual(auto.Receipts(self.store).state['stage'], 'sync-intent')
        self.assertEqual(self.run_auto()['status'], 'baseline-deferred')
        self.ops.sync.assert_called_once()

    def test_unconfirmed_sync_is_never_retried(self):
        self.ops.sync.side_effect = TimeoutError('unknown')
        with self.assertRaises(TimeoutError):
            self.run_auto()
        for _ in range(2):
            self.assertEqual(self.run_auto()['reason'], 'sync-outcome-unknown-no-replay')
        self.ops.sync.assert_called_once()
        self.ops.build.assert_not_called()

    def test_lost_publication_response_recovers_exact_commit_tree_inputs(self):
        self.ops.publish.side_effect = TimeoutError('unknown')
        with self.assertRaises(TimeoutError):
            self.run_auto()
        self.assertEqual(self.run_auto()['status'], 'baseline-deferred')
        self.ops.publish.assert_called_once()
        self.ops.build.assert_called_once()

    def test_same_tree_wrong_commit_cannot_recover_publication(self):
        self.ops.publish.side_effect = TimeoutError('unknown')
        with self.assertRaises(TimeoutError):
            self.run_auto()
        wrong = auto.published_snapshot(self.proof)
        wrong['mirror_branches']['parsed'] = sha('8')
        self.ops.snapshot.return_value = wrong
        self.assertEqual(self.run_auto()['reason'], 'publication-outcome-unknown-no-replay')
        self.ops.deliver.assert_not_called()
        self.ops.publish.assert_called_once()

    def test_equal_tree_build_has_persistent_deferred_receipt(self):
        self.ops.build.return_value = {'status': 'no-change', 'plan': self.final}
        self.assertEqual(self.run_auto()['status'], 'equal-tree-deferred')
        self.assertTrue(self.run_auto()['reused'])
        self.ops.build.assert_called_once()
        self.ops.publish.assert_not_called()
        self.ops.deliver.assert_not_called()

    def test_build_crash_does_not_start_endless_rebuilds(self):
        self.ops.build.side_effect = RuntimeError('build failed')
        with self.assertRaises(RuntimeError):
            self.run_auto()
        self.assertEqual(self.run_auto()['reason'], 'interrupted-build-review-required')
        self.ops.build.assert_called_once()

    def test_ocr_and_helper_changes_block_before_sync(self):
        for name in ('ocr_patch', 'ocr_cache', 'helper', 'patch_layout'):
            with self.subTest(name=name):
                plan = copy.deepcopy(self.initial)
                plan['current_inputs'][name] = 'changed'
                self.ops.plan.side_effect = None
                self.ops.plan.return_value = plan
                self.assertEqual(self.run_auto()['reason'], 'non-regular-input-change')
        self.assertEqual(self.store.writes, [])
        self.ops.sync.assert_not_called()

    def test_upstream_parsed_shortcut_is_deferred_before_input_mutation(self):
        plan = copy.deepcopy(self.initial)
        plan['snapshot']['upstream_branches']['config'] = plan['snapshot']['mirror_branches']['config']
        self.ops.plan.side_effect = None
        self.ops.plan.return_value = plan
        self.assertEqual(self.run_auto()['reason'], 'upstream-parsed-review-required')
        self.assertEqual(self.store.writes, [])

    def test_repository_change_between_selection_and_plan_blocks_writes(self):
        plan = copy.deepcopy(self.initial)
        plan['snapshot']['mirror_repository_id'] = 99
        self.ops.plan.side_effect = None
        self.ops.plan.return_value = plan
        self.assertEqual(self.run_auto()['reason'], 'branch-plan-drift')
        self.assertEqual(self.store.writes, [])
        self.ops.sync.assert_not_called()

    def test_changed_baseline_after_sync_blocks_candidate_generation(self):
        self.final['recorded_inputs'] = {**self.final['recorded_inputs'], 'config': sha('9')}
        self.assertEqual(self.run_auto()['reason'], 'normalized-plan-drift')
        self.ops.build.assert_not_called()

    def test_multiple_changed_archives_only_build_one(self):
        second = copy.deepcopy(self.row)
        second['archive'] = 2
        self.ops.monitor.return_value = {'archives': [self.row, second]}
        self.assertEqual(self.run_auto()['archive'], 0)
        self.ops.build.assert_called_once()
        self.ops.sync.assert_called_once()

    def test_baseline_adoption_releases_handoff_without_new_work_same_run(self):
        self.run_auto()
        self.ops.reset_mock()
        self.ops.adopted.return_value = True
        self.assertEqual(self.run_auto()['status'], 'baseline-adopted')
        self.assertEqual(auto.Receipts(self.store).state['stage'], 'complete')
        self.ops.monitor.assert_not_called()

    def test_failed_index_acceptance_keeps_published_receipt(self):
        self.ops.deliver.return_value = {'status': 'timeout', 'ok': False}
        self.assertEqual(self.run_auto()['status'], 'delivery-pending')
        self.assertEqual(auto.Receipts(self.store).state['stage'], 'published')
        self.ops.baseline.assert_not_called()
        self.run_auto()
        self.ops.publish.assert_called_once()


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.state = {'kind': 'bha-upstream-auto-state', 'version': 1, 'archive': 0, 'stage': 'building'}

    def test_exact_artifact_bytes_and_namespace_are_required(self):
        receipts = auto.Receipts(self.store)
        ref = receipts.retain({'exact': 'artifact'})
        self.assertEqual(receipts.read(ref), {'exact': 'artifact'})
        for field, replacement in [('sha256', '0' * 64), ('bytes', 0), ('bucket', 'vomebook/search-data-v1'), ('key', '../head.json')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipts.read({**ref, field: replacement})
        self.store.data[ref['key']] += b' '
        with self.assertRaises(ValueError):
            receipts.read(ref)

    def test_lost_receipt_and_head_responses_use_readback_no_retry(self):
        self.store.lose = 'all'
        receipts = auto.Receipts(self.store)
        receipts.save(self.state)
        self.assertEqual(auto.Receipts(self.store).state, self.state)
        self.assertEqual(len(self.store.writes), 2)

    def test_competing_identical_intent_does_not_grant_two_writers(self):
        one, two = auto.Receipts(self.store), auto.Receipts(self.store)
        one.save(copy.deepcopy(self.state))
        with self.assertRaisesRegex(RuntimeError, 'conflicting'):
            two.save(copy.deepcopy(self.state))
        self.assertEqual(auto.Receipts(self.store).state, one.state)

    def test_missing_durable_candidate_stops_resume(self):
        receipts = auto.Receipts(self.store)
        ref = receipts.retain({'candidate': 'evidence'})
        receipts.save({**self.state, 'stage': 'publish-intent', 'candidate': ref})
        del self.store.data[ref['key']]
        with self.assertRaises(ValueError):
            auto.run(action='auto', receipts=auto.Receipts(self.store), ops=Mock(), provenance={})

    def test_bundle_validation_rejects_wrong_exact_artifact(self):
        # Use the existing real artifact validator, not a mocked digest check.
        _, _, _, plan, value = fixtures()
        candidate = {**value['candidate'], 'changed': True,
                     'revisions': plan['snapshot']['mirror_branches'],
                     'helper_revision': plan['snapshot']['helper_revision'],
                     'before_tree': plan['snapshot']['parsed_tree']}
        with tempfile.TemporaryDirectory(prefix='bha-auto-artifact-test-') as directory:
            root = Path(directory)
            bundle = root / 'parsed.bundle'
            bundle.write_bytes(b'exact bundle')
            value.update(kind='bha-parsed-candidate-result', remote_writes=0, publication_authorized=False,
                         plan_sha256=plan['plan_sha256'], candidate=candidate,
                         bundle={'filename': bundle.name, 'bytes': bundle.stat().st_size,
                                 'sha256': auto.digest(bundle.read_bytes())})
            path = root / 'candidate.json'
            path.write_bytes(auto.search_snapshot.canonical(value))
            checksum = auto.digest(path.read_bytes())
            self.assertEqual(auto.publication.read_candidate(path, checksum, bundle), value)
            bundle.write_bytes(b'other bundle')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                auto.publication.read_candidate(path, checksum, bundle)
            path.write_bytes(path.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'receipt hash'):
                auto.publication.read_candidate(path, checksum, bundle)


class WorkflowTests(unittest.TestCase):
    def test_workflow_defaults_gates_and_durable_store_contract(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-upstream-auto.yml'
        text = path.read_text()
        self.assertIn('default: inspect', text)
        self.assertIn("vars.BHA_UPSTREAM_AUTO_ENABLED == 'true'", text)
        self.assertIn('group: maintain-archive-forks', text)
        self.assertIn('group: update-bha-index', text)
        self.assertIn('cancel-in-progress: false', text)
        self.assertIn('persist-credentials: false', text)
        self.assertIn('HF_S3_SECRET_ACCESS_KEY:', text)
        self.assertNotIn('bha_baseline_publish', text)
        self.assertNotIn('contents: write', text)
        self.assertNotIn('workflow_run:', text)

    def test_yaml_and_job_level_index_coordination(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-upstream-auto.yml'
        workflow = yaml.safe_load(path.read_text())
        self.assertEqual(workflow['concurrency']['group'], 'maintain-archive-forks')
        job = workflow['jobs']['normalize']
        self.assertEqual(job['concurrency']['group'], 'update-bha-index')
        self.assertFalse(job['concurrency']['cancel-in-progress'])


class DeliveryBoundaryTests(unittest.TestCase):
    def test_adapter_refuses_multi_archive_plan_before_delivery_preparation(self):
        _, _, _, _, value = fixtures()
        proof = auto.published_receipt(value)
        with tempfile.TemporaryDirectory(prefix='bha-auto-delivery-test-') as directory:
            root = Path(directory)
            samples = root / 'samples.json'
            samples.write_text(json.dumps({'samples': [{'archive': 0, 'path': 'sample.json'}]}))
            ops = auto.Operations('not-a-real-token', root, root / 'inputs', root / 'trees', samples)
            plan = {'changed_archives': [0, 2]}
            def run(**kwargs):
                return kwargs['collect_plan']()
            with patch.object(auto.journal, 'S3Store'), patch.object(auto.journal, 'load', return_value=None), \
                    patch.object(auto.delivery, 'inspect_plan_with_samples', return_value=plan), \
                    patch.object(auto.delivery, 'run', side_effect=run):
                with self.assertRaisesRegex(ValueError, 'exceeds one archive'):
                    ops.deliver(proof)


if __name__ == '__main__':
    unittest.main()
