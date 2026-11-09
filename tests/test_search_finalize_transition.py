"""Search finalization never treats one site's success as the other site's receipt."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import search_finalize_transition as subject
from scripts.search_publication_pointer import make_pointer
from tests.test_search_transition import (
    A,
    B,
    SearchTransitionTests,
    default_receipt,
    receipt,
)

REV, TIME, OP = 'd' * 40, '2026-11-08T00:00:00Z', '9' * 32


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        fixture = SearchTransitionTests(); fixture.setUp(); self.store = fixture.s
        self.current = fixture.ready(); self.session, self.api, self.hub = Mock(), Mock(), Mock()
        self.common = {'hub': self.hub, 'api': self.api, 'session': self.session,
                       'get': lambda *_: b'', 'revision': lambda *_: ''}
        self.actual = patch.object(subject, '_actual', return_value=(receipt('pages', B), receipt('space', B)))
        self.actual.start(); self.addCleanup(self.actual.stop)

    def marked(self): return subject.mark_publishing(self.store, **self.common)

    def pointer_observed(self):
        self.marked(); current = subject.publish(self.store, self.store, **self.common)
        self.assertEqual(current.record['state'], 'pointer-observed'); return current

    def test_workflow_is_manual_serialized_and_requires_writer_coordination(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-promotion-finalize.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'daily-sync')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['known_writers_coordinated']['default'], 'false')
        self.assertIn('reconcile-pointer', flow['on']['workflow_dispatch']['inputs']['mode']['options'])
        self.assertIn('pages-default-reconcile', flow['on']['workflow_dispatch']['inputs']['mode']['options'])
        self.assertIn('rollback-finish', flow['on']['workflow_dispatch']['inputs']['mode']['options'])
        self.assertIn('abort-unpublished', flow['on']['workflow_dispatch']['inputs']['mode']['options'])

    def test_actual_prepublication_checks_durable_source_digest(self):
        import hashlib
        from scripts import search_snapshot
        from types import SimpleNamespace
        self.actual.stop()
        revisions={repo:'a'*40 for repo in subject.fetch.REPOS}
        digest=hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()
        current=SimpleNamespace(record={'candidate':B,'source_revisions_sha256':digest})
        self.api.get_space_runtime.return_value=SimpleNamespace(stage='RUNNING',raw={'sha':REV})
        common={**self.common,'revision':lambda *_:'a'*40}
        with patch.object(subject.adoption,'validate_generation',return_value={'source_revisions':revisions}), \
                patch.object(subject.fetch,'get_repo_sha',return_value='a'*40), \
                patch.object(subject.space,'variable',return_value=(B,TIME)), \
                patch.object(subject.space,'runtime'),patch.object(subject.pages,'assert_idle'), \
                patch.object(subject.consumers,'observe',side_effect=lambda site,generation,*_a,**_k:receipt(site,generation)):
            first,second=subject._actual(current,**common)
            self.assertEqual(first,receipt('pages',B));self.assertEqual(second,receipt('space',B))
            current.record['source_revisions_sha256']='f'*64
            with self.assertRaisesRegex(ValueError,'durable intent'):
                subject._actual(current,**common)

    def test_pointer_write_after_both_actual_receipts_and_lost_response(self):
        current = self.marked(); self.assertEqual(current.record['state'], 'publishing')
        self.store.lose_pointer_response = True
        current = subject.publish(self.store, self.store, **self.common)
        self.assertEqual(current.record['state'], 'pointer-observed'); self.assertEqual(self.store.pointer_writes, 1)
        self.assertEqual(self.store.pointer, make_pointer(B, A, '1' * 32))

    def test_lost_pointer_result_is_read_only_reconciled_without_replay(self):
        self.marked(); self.store.pointer = make_pointer(B, A, '1' * 32)
        current = subject.reconcile_pointer(self.store, **self.common)
        self.assertEqual(current.record['state'], 'pointer-observed'); self.assertEqual(self.store.pointer_writes, 0)

    def test_pages_default_write_and_separate_observation_then_finish(self):
        current = self.pointer_observed()
        with patch.object(subject.space, 'runtime'), patch.object(subject.defaults, 'observe_space', return_value=default_receipt('space', B)):
            current = subject.observe_space_default(self.store, **self.common, expected_revision=REV, operation=OP, created_at=TIME)
        self.assertEqual(current.record['defaults']['space']['generation'], B)
        self.session.get.return_value.json.return_value = {'commit': {'sha': REV}}
        self.session.patch.return_value.status_code = 204
        with patch.object(subject.pages, 'assert_idle'), \
                patch.object(subject.defaults, 'observe_pages', side_effect=[default_receipt('pages', A), default_receipt('pages', A),
                                                                            default_receipt('pages', B), default_receipt('pages', B),
                                                                            default_receipt('pages', B)]), \
                patch.object(subject.defaults, 'observe_space', return_value=default_receipt('space', B)):
            current = subject.start_pages_default(self.store, session=self.session, expected_revision=REV,
                                                  operation='8' * 32, created_at=TIME)
            result = subject.set_pages_default(self.store, **self.common, operation='8' * 32)
            self.assertTrue(result['variable_written']); self.assertFalse(result['journal_advanced'])
            self.assertEqual(self.session.patch.call_count, 1)
            current = subject.reconcile_pages_default(self.store, **self.common, operation='8' * 32)
            self.assertEqual(current.record['defaults']['pages']['generation'], B)
            current = subject.finish(self.store, **self.common)
        self.assertEqual(current.record['state'], 'complete')

    def test_space_default_read_only_recovery_after_interruption(self):
        self.pointer_observed()
        with patch.object(subject.space, 'runtime'), \
                patch.object(subject.defaults, 'observe_space', side_effect=TimeoutError('read interrupted')):
            with self.assertRaises(TimeoutError):
                subject.observe_space_default(self.store, **self.common, expected_revision=REV,
                                              operation=OP, created_at=TIME)
        interrupted = subject.tx.load(self.store)
        pending = interrupted.record['pending']
        self.assertEqual(pending['operation_id'], OP)
        with patch.object(subject.space, 'runtime'), \
                patch.object(subject.defaults, 'observe_space', return_value=default_receipt('space', B)):
            current = subject.observe_space_default(self.store, **self.common, expected_revision=REV,
                                                     operation='7' * 32, created_at=TIME)
        self.assertIsNone(current.record['pending'])
        self.assertEqual(current.record['defaults']['space']['generation'], B)
        self.assertEqual(current.record['sequence'], interrupted.record['sequence'] + 1)
        self.session.patch.assert_not_called()

    def test_lost_pages_default_response_cannot_be_patched_again(self):
        self.pointer_observed()
        self.session.get.return_value.json.return_value = {'commit': {'sha': REV}}
        self.session.patch.side_effect = TimeoutError('response lost')
        with patch.object(subject.pages, 'assert_idle'), \
                patch.object(subject.defaults, 'observe_pages', return_value=default_receipt('pages', A)):
            subject.start_pages_default(self.store, session=self.session, expected_revision=REV,
                                        operation='8' * 32, created_at=TIME)
            with self.assertRaises(TimeoutError):
                subject.set_pages_default(self.store, **self.common, operation='8' * 32)
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                subject.set_pages_default(self.store, **self.common, operation='8' * 32)
        self.session.patch.assert_called_once()
        self.assertIsNotNone(subject.tx.load(self.store).record['pending']['write_claim'])
        with patch.object(subject.defaults, 'observe_pages', return_value=default_receipt('pages', B)):
            current = subject.reconcile_pages_default(self.store, **self.common, operation='8' * 32)
        self.assertEqual(current.record['defaults']['pages']['generation'], B)
        self.session.patch.assert_called_once()

    def test_rollback_finish_reads_both_live_consumers_and_defaults(self):
        current = self.current
        for site, op in (('pages', '5' * 32), ('space', '6' * 32)):
            kwargs = {'config_before': {'generation': B, 'updated_at': TIME}} if site == 'space' else {}
            current = subject.tx.start_site(self.store, current, site=site, operation_id=op,
                                            expected_revision=REV, created_at=TIME, rollback=True, **kwargs)
            current = subject.tx.observe_site(self.store, current, site=site, generation=A, receipt=receipt(site, A))
        self.assertEqual(current.record['state'], 'rolling-back')
        self.api.get_space_runtime.return_value = Mock(stage='RUNNING', raw={'sha': REV})
        self.session.get.return_value.json.return_value = {'commit': {'sha': REV}}
        with patch.object(subject.adoption, 'validate_generation', return_value={}), \
                patch.object(subject.pages, 'assert_idle'), patch.object(subject.space, 'runtime'), \
                patch.object(subject.space, 'variable', return_value=(A, TIME)), \
                patch.object(subject.defaults, 'observe_pages', return_value=default_receipt('pages', A)), \
                patch.object(subject.defaults, 'observe_space', return_value=default_receipt('space', A)), \
                patch.object(subject.consumers, 'observe', side_effect=lambda site, generation, *_a, **_k: receipt(site, generation)) as observe:
            result = subject.finish_rollback(self.store, session=self.session, api=self.api, hub=self.hub,
                                             get=lambda *_: b'')
        self.assertEqual(result.record['state'], 'rolled-back')
        self.assertEqual(observe.call_count, 2)
        self.assertEqual(self.store.pointer, make_pointer(A, None, '0' * 32))
        self.session.patch.assert_not_called()
        self.assertEqual(subject.tx.audit_history(self.store, result), 10)

    def test_foreign_or_missing_pending_default_never_writes(self):
        self.pointer_observed()
        with self.assertRaisesRegex(ValueError, 'no exact'):
            subject.set_pages_default(self.store, **self.common, operation='8' * 32)
        self.session.patch.assert_not_called()


if __name__ == '__main__': unittest.main()
