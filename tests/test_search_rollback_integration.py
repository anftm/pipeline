"""Simulated pre-pointer failure recovery; never contacts remote services."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import search_finalize_transition as final
from scripts import search_pages_transition as pages
from scripts import search_space_transition as space
from scripts import search_transition as tx
from tests.test_search_transition import A, B, C, MemoryStore, default_receipt, receipt

REV, TIME = 'd' * 40, '2026-11-08T00:00:00Z'


class SearchRollbackIntegrationTests(unittest.TestCase):
    def test_failed_pages_run_then_space_rollback_and_two_site_final_acceptance(self):
        store, api, session, hub = MemoryStore(), Mock(), Mock(), Mock()
        parent_pointer = store.pointer
        session.get.return_value.json.return_value = {'commit': {'sha': REV}}
        api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': REV})
        variables = {'generation': A, 'updated_at': TIME}
        live = {'space': A, 'pages': A}
        attempts, runs = [], {}

        def update_space(_api, *, expected, expected_updated_at, target, expected_sha):
            self.assertEqual((expected, expected_updated_at, expected_sha),
                             (variables['generation'], variables['updated_at'], REV))
            attempts.append(('space', target))
            variables['generation'] = live['space'] = target
            raise TimeoutError('response lost after Space change')

        def dispatch(_session, *, generation, operation, expected_sha):
            self.assertEqual(expected_sha, REV)
            attempts.append(('pages', generation))
            runs[operation] = {'id': 71, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
            raise TimeoutError('response lost after Pages dispatch')

        def observe(site, generation, _manifest, **_kwargs):
            if live[site] != generation:
                raise ValueError('actual site is not the expected generation')
            return receipt(site, generation, 'fresh')

        tx.begin(store, parent_pointer=parent_pointer, candidate=B, source_revisions_sha256=C,
                 pages_receipt=receipt('pages', A), space_receipt=receipt('space', A),
                 pages_default=default_receipt('pages', A), space_default=default_receipt('space', A),
                 transition='1' * 32)
        with patch.object(space, '_verify_candidate_sources'), \
                patch.object(space.deploy, 'runtime', return_value={'sha': REV}), \
                patch.object(space.deploy, 'variable', side_effect=lambda _api: (variables['generation'], variables['updated_at'])), \
                patch.object(space.deploy, 'set_generation', side_effect=update_space), \
                patch.object(pages, '_main', return_value=REV), \
                patch.object(pages, '_verify_candidate_sources'), \
                patch.object(pages.deploy, 'assert_idle'), \
                patch.object(pages.deploy, 'failed_before_deploy'), \
                patch.object(pages.deploy, 'dispatch', side_effect=dispatch), \
                patch.object(pages.deploy, 'inspect_runs', side_effect=lambda _s, *, operation, **_k: runs.get(operation)), \
                patch.object(pages.defaults, 'observe_pages', return_value=default_receipt('pages', A)), \
                patch.object(final.defaults, 'observe_space', side_effect=lambda generation, **_k: default_receipt('space', generation)), \
                patch.object(pages.adoption, 'validate_generation', side_effect=lambda _hub, generation: {'generation': generation}), \
                patch.object(pages.receipts, 'observe', side_effect=observe):
            space.prepare(store, api=api, expected_revision=REV, operation_id='2' * 32, created_at=TIME)
            with self.assertRaises(TimeoutError):
                space.apply_once(store, api=api, operation_id='2' * 32, hub=hub)
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                space.apply_once(store, api=api, operation_id='2' * 32, hub=hub)
            self.assertEqual(space.reconcile(store, api=api, hub=hub, get=None, operation_id='2' * 32)['status'],
                             'candidate-loaded')
            pages.prepare(store, session=session, api=api, hub=hub, get=None, expected_revision=REV,
                          operation_id='3' * 32, created_at=TIME)
            with self.assertRaises(TimeoutError):
                pages.apply_once(store, session=session, api=api, hub=hub, get=None, operation_id='3' * 32)
            with self.assertRaisesRegex(ValueError, 'reconcile instead'):
                pages.apply_once(store, session=session, api=api, hub=hub, get=None, operation_id='3' * 32)
            pages.recover_failed_apply(store, session=session, hub=hub, get=None, operation_id='3' * 32)
            self.assertEqual(tx.load(store).record['state'], 'rolling-back')
            space.prepare(store, api=api, expected_revision=REV, operation_id='4' * 32,
                          created_at=TIME, rollback=True, hub=hub, get=lambda *_: b'')
            with self.assertRaises(TimeoutError):
                space.apply_once(store, api=api, operation_id='4' * 32, hub=hub)
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                space.apply_once(store, api=api, operation_id='4' * 32, hub=hub)
            self.assertEqual(space.reconcile(store, api=api, hub=hub, get=None, operation_id='4' * 32)['status'],
                             'parent-loaded')
            self.assertEqual(tx.load(store).record['state'], 'rolling-back')
            result = final.finish_rollback(store, session=session, api=api, hub=hub, get=None)

        self.assertEqual(result.record['state'], 'rolled-back')
        self.assertEqual(result.record['observed']['pages']['generation'], A)
        self.assertEqual(result.record['observed']['space']['generation'], A)
        self.assertEqual(tx.audit_history(store, result), 11)
        self.assertEqual(attempts, [('space', B), ('pages', B), ('space', A)])
        self.assertEqual(store.pointer, parent_pointer)
        self.assertEqual(store.pointer_writes, 0)
        self.assertEqual(live, {'space': A, 'pages': A})

    def test_two_site_rollback_in_either_order_preserves_pointer_until_final(self):
        for order in (('pages', 'space'), ('space', 'pages')):
            with self.subTest(order=order):
                store, api, session, hub = MemoryStore(), Mock(), Mock(), Mock()
                parent_pointer = store.pointer
                session.get.return_value.json.return_value = {'commit': {'sha': REV}}
                api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': REV})
                variables, live, runs = {'generation': B}, {'pages': B, 'space': B}, {}
                c = tx.begin(store, parent_pointer=parent_pointer, candidate=B, source_revisions_sha256=C,
                             pages_receipt=receipt('pages', A), space_receipt=receipt('space', A),
                             pages_default=default_receipt('pages', A), space_default=default_receipt('space', A),
                             transition='1' * 32)
                for site, op in (('space', '2' * 32), ('pages', '3' * 32)):
                    config = {'generation': A, 'updated_at': TIME} if site == 'space' else None
                    c = tx.start_site(store, c, site=site, operation_id=op, expected_revision=REV,
                                      created_at=TIME, config_before=config)
                    c = tx.observe_site(store, c, site=site, generation=B, receipt=receipt(site, B))

                def dispatch(_session, *, generation, operation, expected_sha):
                    self.assertEqual((generation, expected_sha), (A, REV))
                    live['pages'] = generation
                    runs[operation] = {'id': 72, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 1}

                def update_space(_api, *, expected, expected_updated_at, target, expected_sha):
                    self.assertEqual((expected, expected_updated_at, target, expected_sha), (B, TIME, A, REV))
                    variables['generation'] = live['space'] = target

                def observe(site, generation, _manifest, **_kwargs):
                    if live[site] != generation:
                        raise ValueError('actual site differs')
                    return receipt(site, generation, 'actual')

                with patch.object(space.deploy, 'runtime', return_value={'sha': REV}), \
                        patch.object(space.deploy, 'variable', side_effect=lambda _api: (variables['generation'], TIME)), \
                        patch.object(space.deploy, 'set_generation', side_effect=update_space), \
                        patch.object(pages, '_main', return_value=REV), \
                        patch.object(pages.deploy, 'assert_idle'), \
                        patch.object(pages.deploy, 'dispatch', side_effect=dispatch), \
                        patch.object(pages.deploy, 'inspect_runs', side_effect=lambda _s, *, operation, **_k: runs.get(operation)), \
                        patch.object(pages.defaults, 'observe_pages', return_value=default_receipt('pages', A)), \
                        patch.object(final.defaults, 'observe_space', return_value=default_receipt('space', A)), \
                        patch.object(pages.adoption, 'validate_generation', side_effect=lambda _hub, generation: {'generation': generation}), \
                        patch.object(pages.receipts, 'observe', side_effect=observe):
                    for site in order:
                        op = '4' * 32 if site == 'pages' else '5' * 32
                        if site == 'pages':
                            pages.prepare(store, session=session, api=api, hub=hub, get=None, expected_revision=REV,
                                          operation_id=op, created_at=TIME, rollback=True)
                            pages.apply_once(store, session=session, api=api, hub=hub, get=None, operation_id=op)
                            pages.reconcile(store, session=session, api=api, hub=hub, get=None, operation_id=op)
                        else:
                            space.prepare(store, api=api, expected_revision=REV, operation_id=op,
                                          created_at=TIME, rollback=True, hub=hub, get=lambda *_: b'')
                            space.apply_once(store, api=api, operation_id=op, hub=hub)
                            space.reconcile(store, api=api, hub=hub, get=None, operation_id=op)
                        self.assertEqual(tx.load(store).record['state'], 'rolling-back')
                    result = final.finish_rollback(store, session=session, api=api, hub=hub, get=None)
                self.assertEqual(result.record['state'], 'rolled-back')
                self.assertEqual(store.pointer, parent_pointer)
                self.assertEqual(store.pointer_writes, 0)
                self.assertEqual(live, {'pages': A, 'space': A})
                self.assertEqual(tx.audit_history(store, result), 12)

    def test_unknown_space_apply_stays_pending_and_does_not_allow_rollback(self):
        store, api = MemoryStore(), Mock()
        tx.begin(store, parent_pointer=store.pointer, candidate=B, source_revisions_sha256=C,
                 pages_receipt=receipt('pages', A), space_receipt=receipt('space', A),
                 pages_default=default_receipt('pages', A), space_default=default_receipt('space', A),
                 transition='1' * 32)
        with patch.object(space, '_verify_candidate_sources'), \
                patch.object(space.adoption, 'validate_generation', return_value={}), \
                patch.object(space.deploy, 'runtime'), \
                patch.object(space.deploy, 'variable', return_value=(A, TIME)), \
                patch.object(space.deploy, 'set_generation', side_effect=TimeoutError('unknown')) as send:
            space.prepare(store, api=api, expected_revision=REV, operation_id='2' * 32, created_at=TIME)
            with self.assertRaises(TimeoutError):
                space.apply_once(store, api=api, operation_id='2' * 32, hub=Mock())
            result = space.reconcile(store, api=api, hub=Mock(), get=None, operation_id='2' * 32)
            self.assertEqual(result['status'], 'parent-configured')
            with self.assertRaisesRegex(ValueError, 'not ready'):
                space.prepare(store, api=api, expected_revision=REV, operation_id='3' * 32,
                              created_at=TIME, rollback=True, hub=Mock(), get=lambda *_: b'')
        send.assert_called_once()
        self.assertEqual(tx.load(store).record['pending']['operation_id'], '2' * 32)
        self.assertEqual(store.pointer_writes, 0)


if __name__ == '__main__':
    unittest.main()
