"""Characterize legacy retry hazards with synthetic mocks; do not enable them.

These tests intentionally expose current unsafe assumptions, not desired
contracts. Update them when a separately reviewed recovery design replaces the
legacy behavior. No request, branch, PR or repository is read or written.
"""

import unittest
from unittest.mock import patch

from scripts import submit_proofread as legacy


class LegacyRecoveryRiskTests(unittest.TestCase):
    def test_pr_lookup_errors_are_currently_indistinguishable_from_absence(self):
        for status, body in ((403, {'message': 'forbidden'}), (429, {}), (500, {}), (200, {})):
            with self.subTest(status=status), \
                    patch.object(legacy, 'api_request', return_value=(status, body)) as api, \
                    patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                # Characterization only: an unavailable/incomplete lookup is
                # not proof that no previous PR exists and cannot justify retry.
                self.assertIsNone(legacy.open_pull_request('fixture-token', 'fixture-repo', 'proofread/fixture'))
                self.assertEqual(api.call_count, 1)
                self.assertEqual(api.call_args.args[1], 'GET')

    def test_existing_branch_without_visible_pr_can_receive_unverified_writes(self):
        for mode in ('single', 'batch'):
            with self.subTest(mode=mode):
                writes = []

                def get_file(_token, _repo, branch, _path):
                    return ('base-content', 'base-file-sha') if branch == 'config' else ('foreign-content', 'branch-file-sha')

                def response(_token, method, path, _expected, payload=None):
                    writes.append((method, path, payload))
                    if method == 'POST' and path.endswith('/pulls'):
                        return {'number': 1, 'html_url': 'https://example.invalid/pr/1',
                                'head': {'sha': 'synthetic-head'}}
                    return {}

                with patch.object(legacy, 'api_request', return_value=(403, {'message': 'forbidden'})), \
                        patch.object(legacy, 'branch_sha', return_value='unverified-existing-branch'), \
                        patch.object(legacy, 'get_file', side_effect=get_file), \
                        patch.object(legacy, 'response_or_fail', side_effect=response), \
                        patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                    if mode == 'single':
                        legacy.submit_file('fixture-token', 'fixture-repo', 'config', 'fixture.ts',
                                           'requested-content', 'title', 'description', 'fixture')
                    else:
                        legacy.submit_files('fixture-token', 'fixture-repo', 'config',
                                            {'fixture.ts': 'requested-content'}, 'title', 'description', 'fixture')
                # An existing branch is reused without checking its authorship,
                # base commit or other files. A file SHA on the subsequent PUT
                # does not bind those missing identities or prior PR lookup.
                self.assertEqual([method for method, _path, _payload in writes], ['PUT', 'POST'])
                self.assertEqual(writes[0][2]['sha'], 'branch-file-sha')
                self.assertEqual(writes[0][2]['branch'], 'proofread/fixture-config')


if __name__ == '__main__':
    unittest.main()
