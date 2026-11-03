import unittest
from unittest.mock import Mock

from scripts import reader_producer_v2_names as names


class RetiredNameTests(unittest.TestCase):
    def getter(self, owner_status=200, named_status=404, id_status=404):
        def get(url, *, headers, timeout, follow_redirects):
            token = headers['Authorization'].removeprefix('Bearer ')
            response = Mock()
            if url.endswith('/user'):
                response.status_code = owner_status
                response.json.return_value = {'login': token.split('/')[0]}
            elif '/repos/' in url:
                response.status_code = named_status
            else:
                response.status_code = id_status
            return response
        return get

    def test_all_owners_must_authenticate_and_observe_exact_absence(self):
        result = names.check(get=self.getter(), token_for=lambda repo: repo)
        self.assertEqual(len(result['repositories']), 9)
        self.assertFalse(result['source_credentials_revoked'])
        self.assertFalse(result['external_writers_fenced'])
        self.assertFalse(result['gc_authorized'])

    def test_owner_authentication_and_name_or_id_drift_fail_closed(self):
        for statuses in ((401, 404, 404), (403, 404, 404), (200, 200, 404),
                         (200, 404, 200), (200, 403, 404), (200, 404, 500)):
            with self.subTest(statuses=statuses), self.assertRaises(RuntimeError):
                names.check(get=self.getter(*statuses), token_for=lambda repo: repo)

    def test_credential_failure_does_not_fallback_to_another_owner(self):
        called = []
        def missing(repo):
            called.append(repo)
            raise RuntimeError('missing')
        with self.assertRaises(RuntimeError):
            names.check(get=self.getter(), token_for=missing)
        self.assertEqual(len(called), 1)


if __name__ == '__main__':
    unittest.main()
