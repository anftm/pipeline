import unittest
from unittest.mock import Mock, patch

from scripts import reader_producer_v2_credentials_preflight as check


class CredentialPreflightTests(unittest.TestCase):
    def get(self, url, *, headers, params, timeout, follow_redirects):
        response = Mock(status_code=200)
        if url.endswith('/repos/anftm/pipeline'):
            value = {'id': 1260985830, 'full_name': 'anftm/pipeline'}
        elif url.endswith('/actions/workflows'):
            value = {'total_count': 30}
        elif url.endswith('/actions/runs'):
            value = {'total_count': 0}
        else:
            raise AssertionError(url)
        response.json.return_value = value
        return response

    def test_central_credential_reads_identity_and_all_run_statuses(self):
        result = check.central_check(get=self.get, token_for=lambda _: 'synthetic')
        self.assertEqual(result['repository_id'], 1260985830)
        self.assertEqual(set(result['run_counts']), set(check.STATUSES))

    def test_bad_auth_or_identity_fails_without_leaking_token(self):
        for status in (401, 403, 404, 500):
            response = Mock(status_code=status)
            with self.subTest(status=status), self.assertRaises(RuntimeError) as error:
                check.central_check(get=lambda *a, **k: response, token_for=lambda _: 'synthetic-secret')
            self.assertNotIn('synthetic-secret', str(error.exception))

    def test_missing_workflow_context_blocks_all_reads(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(check, 'central_check') as central:
            with self.assertRaisesRegex(RuntimeError, 'central workflow'):
                check.run()
        central.assert_not_called()


if __name__ == '__main__':
    unittest.main()
