import unittest
from unittest.mock import Mock, patch

from scripts import reader_producer_v2_shadow as shadow


class ShadowTests(unittest.TestCase):
    def api(self, endpoint):
        if endpoint == 'repos/anftm/pipeline':
            return [{'id': shadow.CENTRAL_ID, 'full_name': shadow.CENTRAL}]
        if '/actions/workflows?' in endpoint:
            return [{'workflows': [{'id': 1, 'path': '.github/workflows/pdf-account-worker.yml',
                                    'state': 'disabled_manually'}]}]
        if '/actions/runs?status=' in endpoint:
            return [{'workflow_runs': []}]
        raise AssertionError(endpoint)

    def test_central_identity_and_workflow_state_are_checked(self):
        report = shadow.check_central(self.api)
        self.assertEqual(report['repository_id'], shadow.CENTRAL_ID)
        self.assertEqual(report['workflow_count'], 1)
        self.assertEqual(report['enabled_workflows'], [])

    def test_active_workflow_or_foreign_repository_blocks(self):
        for mode in ('active', 'foreign'):
            def api(endpoint):
                result = self.api(endpoint)
                if mode == 'active' and '/actions/workflows?' in endpoint:
                    result[0]['workflows'][0]['state'] = 'active'
                if mode == 'foreign' and endpoint == 'repos/anftm/pipeline':
                    result[0]['id'] += 1
                return result
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                shadow.check_central(api)

    def test_active_run_blocks(self):
        def api(endpoint):
            if '/actions/runs?status=in_progress' in endpoint:
                return [{'workflow_runs': [{'id': 7, 'status': 'in_progress',
                                            'path': '.github/workflows/reader-chm-repair.yml'}]}]
            return self.api(endpoint)
        with self.assertRaisesRegex(RuntimeError, 'have not drained'):
            shadow.check_central(api)

    def test_shadow_never_authorizes_gc_even_if_everything_checks(self):
        with patch.object(shadow, 'verify_evidence', return_value={'object_count': 12}), \
             patch.object(shadow, 'check_central', return_value={'repository': shadow.CENTRAL}):
            result = shadow.check(Mock(), self.api)
        self.assertFalse(result['external_writers_fenced'])
        self.assertFalse(result['source_credentials_revoked'])
        self.assertFalse(result['scope_activated'])
        self.assertFalse(result['gc_authorized'])

    def test_deleted_name_checks_detect_recreation_without_claiming_isolation(self):
        report = shadow.check_deleted_names(lambda _repo, _rid: (404, 404))
        self.assertEqual(len(report['repositories']), 9)
        self.assertFalse(report['absence_is_isolation_proof'])
        for statuses in ((200, 404), (404, 200), (403, 404), (404, 500)):
            with self.subTest(statuses=statuses), self.assertRaises(RuntimeError):
                shadow.check_deleted_names(lambda _repo, _rid: statuses)


if __name__ == '__main__':
    unittest.main()
