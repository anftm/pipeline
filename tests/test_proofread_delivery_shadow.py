"""Synthetic tests only: no GitHub, HF, Actions, storage adapters or user data."""

import ast
import copy
import json
from pathlib import Path
import unittest

from scripts import proofread_delivery_shadow as shadow


class DeliveryShadowTests(unittest.TestCase):
    def setUp(self):
        self.capability = b'c' * 32
        self.plan = {
            'version': 1, 'scope': 'synthetic-parse-pr-tracker-only',
            'submission_id': '1' * 64, 'payload_sha256': '2' * 64, 'source_sha256': '3' * 64,
            'run': {'repository_id': 303, 'repository_sha': 'e' * 40,
                    'workflow_path': '.github/workflows/proofread-submit-parse.yml',
                    'run_id': 1, 'attempt': 1},
            'config_pr': {'repository_id': 101, 'base_ref': 'config', 'base_sha': 'a' * 40,
                          'head_ref': 'proofread/fixture-config', 'head_sha': 'b' * 40,
                          'files_sha256': '4' * 64},
            'patch_pr': {'repository_id': 101, 'base_ref': 'ocr_patch', 'base_sha': 'c' * 40,
                         'head_ref': 'proofread/fixture-ocr-patch', 'head_sha': 'd' * 40,
                         'files_sha256': '5' * 64},
            'tracker': {'repository_id': 202, 'body_sha256': '6' * 64,
                        'comment_sha256': ['7' * 64, '8' * 64]},
            'receipt_verifier': shadow.receipt_verifier(self.capability),
        }
        self.journal = shadow.new_journal(self.plan)
        self.plan_sha = self.journal['plan_sha256']
        self.revision, self.head = 0, self.plan_sha

    def replay(self):
        return shadow.replay(self.journal, self.plan_sha, self.revision, self.head)

    def append(self, kind, **fields):
        event = {'type': kind, **fields}
        self.journal = shadow.append(self.journal, self.plan_sha, self.revision, self.head, event)
        self.revision += 1
        self.head = self.journal['events'][-1]['event_sha256']

    def claim(self):
        self.append('claim', owner='worker-one', epoch=1)

    def effect(self, kind, effect, **fields):
        self.append(kind, effect=effect, owner='worker-one', epoch=1, **fields)

    def evidence(self, effect):
        base = {key: self.plan[key] for key in ('submission_id', 'payload_sha256', 'source_sha256')}
        base.update(plan_sha256=self.plan_sha, effect=effect, inventory_complete=True)
        if effect in ('config_pr', 'patch_pr'):
            base.update(self.plan[effect], number={'config_pr': 11, 'patch_pr': 12}[effect], state='open')
        elif effect == 'tracker_issue':
            base.update(repository_id=202, body_sha256='6' * 64, number=31, state='open',
                        config_number=11, patch_number=12)
        else:
            index = int(effect.partition(':')[2])
            base.update(repository_id=202, issue_number=31, number=51 + index,
                        body_sha256=self.plan['tracker']['comment_sha256'][index])
        return base

    def verify_next(self, effect):
        self.effect('begin', effect)
        self.effect('verify', effect, evidence=self.evidence(effect))

    def receipt(self, capability=None):
        return shadow.receipt(self.journal, self.plan_sha, self.revision, self.head,
                              self.capability if capability is None else capability)

    def assert_no_production_authority(self):
        report = self.replay()
        for key in ('production_recovery_authorized', 'production_retry_authorized',
                    'submission_open_authorized', 'external_writers_fenced', 'external_effects_proved'):
            self.assertIs(report[key], False, key)

    def test_partial_parse_and_tracker_require_individual_verified_evidence(self):
        self.assertEqual(self.receipt()['status'], 'queued')
        self.claim()
        self.assertEqual(self.receipt()['status'], 'claimed')
        self.verify_next('config_pr')
        self.assertEqual(self.receipt()['status'], 'partial')
        self.effect('begin', 'patch_pr')
        self.effect('ack', 'patch_pr')
        self.assertEqual(self.receipt()['status'], 'verification_pending')
        self.effect('verify', 'patch_pr', evidence=self.evidence('patch_pr'))
        self.verify_next('tracker_issue')
        self.verify_next('tracker_comment:0')
        self.assertEqual(self.receipt()['status'], 'partial')
        self.verify_next('tracker_comment:1')
        self.assertEqual(self.receipt()['status'], 'awaiting_review')
        self.assert_no_production_authority()
        with self.assertRaisesRegex(RuntimeError, 'cannot authorize'):
            shadow.require_real_activation(self.journal)

    def test_timeouts_and_absent_observations_never_make_an_effect_retryable(self):
        for interruption in ('in_flight', 'timeout', 'ack'):
            with self.subTest(interruption=interruption):
                self.setUp()
                self.claim()
                self.verify_next('config_pr')
                self.effect('begin', 'patch_pr')
                if interruption in ('timeout', 'ack'):
                    self.effect(interruption, 'patch_pr')
                self.effect('observe_absent', 'patch_pr')
                self.assertEqual(self.receipt()['status'], 'uncertain')
                with self.assertRaisesRegex(ValueError, 'cannot begin'):
                    self.effect('begin', 'patch_pr')
                with self.assertRaisesRegex(ValueError, 'cannot begin'):
                    self.effect('begin', 'tracker_issue')
                self.effect('verify', 'patch_pr', evidence=self.evidence('patch_pr'))
                self.assertEqual(self.receipt()['status'], 'partial')
                self.assert_no_production_authority()

    def test_partial_failure_and_foreign_conflict_stops_every_later_effect(self):
        self.claim()
        self.verify_next('config_pr')
        self.effect('begin', 'patch_pr')
        self.effect('conflict', 'patch_pr')
        self.assertEqual(self.receipt()['status'], 'blocked')
        with self.assertRaises(ValueError):
            self.effect('verify', 'patch_pr', evidence=self.evidence('patch_pr'))
        with self.assertRaises(ValueError):
            self.effect('begin', 'tracker_issue')
        self.assertEqual(self.replay()['objects'], {'config_pr': 11})
        self.assert_no_production_authority()

    def test_evidence_requires_exact_pr_identity_and_complete_file_inventory(self):
        self.claim()
        self.effect('begin', 'config_pr')
        original = self.evidence('config_pr')
        changes = {
            'submission_id': 'f' * 64, 'payload_sha256': 'f' * 64, 'source_sha256': 'f' * 64,
            'plan_sha256': 'f' * 64, 'effect': 'patch_pr', 'repository_id': True,
            'base_ref': 'other', 'base_sha': 'f' * 40, 'head_ref': 'proofread/foreign',
            'head_sha': 'f' * 40, 'files_sha256': 'f' * 64, 'number': True,
            'state': 'merged', 'inventory_complete': False,
        }
        for key, value in changes.items():
            with self.subTest(field=key):
                evidence = {**original, key: value}
                with self.assertRaises(ValueError):
                    self.effect('verify', 'config_pr', evidence=evidence)
        for evidence in ({**original, 'extra': 'foreign'},
                         {key: value for key, value in original.items() if key != 'files_sha256'}):
            with self.assertRaises(ValueError):
                self.effect('verify', 'config_pr', evidence=evidence)
        self.assertEqual(self.receipt()['status'], 'verification_pending')
        self.effect('verify', 'config_pr', evidence=original)

    def test_tracker_requires_both_correct_prs_and_exact_comments(self):
        self.claim()
        self.verify_next('config_pr')
        self.verify_next('patch_pr')
        self.effect('begin', 'tracker_issue')
        issue = self.evidence('tracker_issue')
        for key, value in (('repository_id', 101), ('config_number', 12), ('patch_number', 13),
                           ('body_sha256', 'f' * 64), ('inventory_complete', False)):
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.effect('verify', 'tracker_issue', evidence={**issue, key: value})
        self.effect('verify', 'tracker_issue', evidence=issue)
        self.effect('begin', 'tracker_comment:0')
        comment = self.evidence('tracker_comment:0')
        for key, value in (('issue_number', 32), ('number', 0), ('body_sha256', 'f' * 64),
                           ('inventory_complete', False)):
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.effect('verify', 'tracker_comment:0', evidence={**comment, key: value})
        self.effect('verify', 'tracker_comment:0', evidence=comment)
        self.effect('begin', 'tracker_comment:1')
        with self.assertRaises(ValueError):
            self.effect('verify', 'tracker_comment:1', evidence={**self.evidence('tracker_comment:1'), 'number': 51})
        self.effect('verify', 'tracker_comment:1', evidence=self.evidence('tracker_comment:1'))

    def test_deduplication_survives_serialization_and_rejects_identity_collisions(self):
        self.claim()
        self.verify_next('config_pr')
        self.journal = json.loads(json.dumps(self.journal))  # synthetic process restart
        identity = (self.plan['submission_id'], self.plan['payload_sha256'], self.plan['source_sha256'])
        result = shadow.classify_duplicate(self.journal, self.plan_sha, self.revision, self.head, *identity)
        self.assertTrue(result['duplicate'])
        self.assertFalse(result['retry_authorized'])
        for index in range(3):
            changed = list(identity)
            changed[index] = 'f' * 64
            with self.subTest(field=index), self.assertRaisesRegex(ValueError, 'identity conflict'):
                shadow.classify_duplicate(self.journal, self.plan_sha, self.revision, self.head, *changed)
        self.assertEqual(self.receipt()['status'], 'partial')

    def test_receipt_requires_capability_and_exposes_no_payload_or_objects(self):
        self.claim()
        self.verify_next('config_pr')
        status = self.receipt()
        self.assertEqual(set(status), {'status', 'revision', 'synthetic', 'production_recovery_authorized'})
        self.assertNotIn('config_pr', str(status))
        self.assertNotIn(self.plan['payload_sha256'], str(status))
        for capability in (b'x' * 32, b'', None, 'c' * 32):
            with self.subTest(capability=capability), self.assertRaisesRegex(LookupError, 'unavailable'):
                shadow.receipt(self.journal, self.plan_sha, self.revision, self.head, capability)

    def test_every_effect_boundary_blocks_blind_replay_after_ack_timeout_or_restart(self):
        effects = ['config_pr', 'patch_pr', 'tracker_issue', 'tracker_comment:0', 'tracker_comment:1']
        for position, target in enumerate(effects):
            for response in ('no_response', 'ack', 'timeout'):
                with self.subTest(effect=target, response=response):
                    self.setUp()
                    self.claim()
                    for effect in effects[:position]:
                        self.verify_next(effect)
                    self.effect('begin', target)
                    if response != 'no_response':
                        self.effect(response, target)
                    self.journal = json.loads(json.dumps(self.journal))
                    expected = 'uncertain' if response == 'timeout' else 'verification_pending'
                    self.assertEqual(self.receipt()['status'], expected)
                    before = copy.deepcopy(self.journal)
                    with self.assertRaises(ValueError):
                        self.effect('begin', target)
                    with self.assertRaises(ValueError):
                        self.append('claim', owner='worker-two', epoch=2)
                    self.assertEqual(self.journal, before)
                    self.effect('observe_absent', target)
                    self.assertEqual(self.receipt()['status'], 'uncertain')
                    self.assert_no_production_authority()
                    self.effect('verify', target, evidence=self.evidence(target))
                    self.assert_no_production_authority()

    def test_replayed_stale_effect_evidence_and_duplicate_pr_number_are_rejected(self):
        self.claim()
        self.verify_next('config_pr')
        self.effect('begin', 'patch_pr')
        for evidence in (self.evidence('config_pr'), {**self.evidence('patch_pr'), 'number': 11}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.effect('verify', 'patch_pr', evidence=evidence)
        self.effect('verify', 'patch_pr', evidence=self.evidence('patch_pr'))

    def test_event_rehashing_cannot_bypass_transition_rules_at_a_supplied_new_head(self):
        self.claim()
        self.effect('begin', 'config_pr')
        self.effect('ack', 'config_pr')
        forged = copy.deepcopy(self.journal)
        forged['events'][-1]['type'] = 'begin'
        event = forged['events'][-1]
        event['event_sha256'] = shadow._digest({key: value for key, value in event.items()
                                                if key != 'event_sha256'})
        with self.assertRaisesRegex(ValueError, 'cannot begin'):
            shadow.replay(forged, self.plan_sha, self.revision, event['event_sha256'])

    def test_checkpoint_detects_truncation_reordering_and_rehashed_history(self):
        self.claim()
        self.verify_next('config_pr')
        examples = []
        truncated = copy.deepcopy(self.journal)
        truncated['events'].pop()
        examples.append(truncated)
        reordered = copy.deepcopy(self.journal)
        reordered['events'][0], reordered['events'][1] = reordered['events'][1], reordered['events'][0]
        examples.append(reordered)
        rehashed = copy.deepcopy(self.journal)
        rehashed['events'][0]['owner'] = 'foreign'
        previous = self.plan_sha
        for event in rehashed['events']:
            event['previous_sha256'] = previous
            event['event_sha256'] = shadow._digest({key: value for key, value in event.items()
                                                    if key != 'event_sha256'})
            previous = event['event_sha256']
        examples.append(rehashed)
        for candidate in examples:
            with self.subTest(events=candidate['events']), self.assertRaises(ValueError):
                shadow.replay(candidate, self.plan_sha, self.revision, self.head)
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            shadow.replay(self.journal, self.plan_sha, self.revision, 'f' * 64)

    def test_rehashed_plan_and_cross_scope_data_are_rejected(self):
        self.claim()
        altered = copy.deepcopy(self.journal)
        altered['plan']['payload_sha256'] = 'f' * 64
        altered['plan_sha256'] = shadow._digest(altered['plan'])
        with self.assertRaisesRegex(ValueError, 'plan digest'):
            shadow.replay(altered, self.plan_sha, self.revision, self.head)
        for mutate in ('scope', 'comments', 'unknown', 'repository', 'base', 'branch', 'workflow', 'run'):
            plan = copy.deepcopy(self.plan)
            if mutate == 'scope':
                plan['scope'] = 'parse-production'
            elif mutate == 'comments':
                plan['tracker']['comment_sha256'] = ['7' * 64, '7' * 64]
            elif mutate == 'unknown':
                plan['unexpected'] = True
            elif mutate == 'repository':
                plan['patch_pr']['repository_id'] = 99
            elif mutate == 'base':
                plan['patch_pr']['base_ref'] = 'config'
            elif mutate == 'branch':
                plan['patch_pr']['head_ref'] = plan['config_pr']['head_ref']
            elif mutate == 'workflow':
                plan['run']['workflow_path'] = '.github/workflows/proofread-submit.yml'
            else:
                plan['run']['run_id'] = True
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                shadow.new_journal(plan)

    def test_claim_and_revision_checks_do_not_claim_real_concurrency_isolation(self):
        initial = copy.deepcopy(self.journal)
        event_a = {'type': 'claim', 'owner': 'worker-one', 'epoch': 1}
        event_b = {'type': 'claim', 'owner': 'worker-two', 'epoch': 1}
        fork_a = shadow.append(initial, self.plan_sha, 0, self.plan_sha, event_a)
        fork_b = shadow.append(initial, self.plan_sha, 0, self.plan_sha, event_b)
        self.assertNotEqual(fork_a['events'][-1]['event_sha256'], fork_b['events'][-1]['event_sha256'])
        self.journal = fork_a
        self.revision, self.head = 1, fork_a['events'][-1]['event_sha256']
        with self.assertRaises(ValueError):
            shadow.append(self.journal, self.plan_sha, 0, self.plan_sha, event_b)
        with self.assertRaises(ValueError):
            self.append('begin', effect='config_pr', owner='worker-two', epoch=1)
        self.assert_no_production_authority()

    def test_invalid_events_are_rejected_without_changing_journal(self):
        before = copy.deepcopy(self.journal)
        for event in ({'type': 'begin', 'effect': 'config_pr', 'owner': 'worker-one', 'epoch': 1},
                      {'type': 'claim', 'owner': 'worker-one', 'epoch': True},
                      {'type': 'claim', 'owner': 'worker-one', 'epoch': 1, 'seq': 7},
                      {'type': []}, {'type': 'unknown'}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                shadow.append(self.journal, self.plan_sha, self.revision, self.head, event)
            self.assertEqual(self.journal, before)
        self.claim()
        for effect in ('tracker_issue', 'tracker_comment:0', 'patch_pr', 'unknown'):
            with self.subTest(effect=effect), self.assertRaises(ValueError):
                self.effect('begin', effect)

    def test_model_has_no_external_write_or_activation_entrypoint(self):
        source = Path(shadow.__file__).read_text()
        tree = ast.parse(source)
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(imports & {'os', 'sys', 'httpx', 'requests', 'boto3', 'subprocess', 'pathlib', 'socket'})
        self.assertFalse(any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree)))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id in {'open', 'exec', 'eval', 'compile'} for node in ast.walk(tree)))
        self.assertNotIn("if __name__ == '__main__'", source)
        workflows = Path(shadow.__file__).resolve().parents[1] / '.github/workflows'
        self.assertFalse([path for path in workflows.glob('*.yml') if 'proofread_delivery_shadow.py' in path.read_text()])


if __name__ == '__main__':
    unittest.main()
