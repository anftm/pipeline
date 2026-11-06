"""Fail-closed protections for legacy PR recovery, using synthetic mocks only.

These tests do not establish idempotency, writer isolation, reliable delivery
or permission to resume submissions. No network or real repository is touched.
"""

import unittest
import os
from unittest.mock import patch

from scripts import submit_proofread as legacy


class LegacyRecoveryRiskTests(unittest.TestCase):
    def test_real_entrypoint_is_paused_even_with_resume_environment_flags(self):
        for flags in ({}, {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'},
                      {'GH_PAT': 'fixture-token', 'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'}):
            with self.subTest(flags=flags), patch.dict(os.environ, flags, clear=True), \
                    patch.object(legacy, 'load_request', side_effect=AssertionError('request read forbidden')), \
                    patch.object(legacy, 'api_request', side_effect=AssertionError('network forbidden')), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                with self.assertRaisesRegex(RuntimeError, 'delivery is paused'):
                    legacy.main()

    def test_pr_lookup_errors_and_incomplete_responses_do_not_mean_absence(self):
        responses = ((403, {'message': 'forbidden'}), (429, {}), (500, {}),
                     (200, {}), (200, [None] * 100))
        for status, body in responses:
            with self.subTest(status=status, body=type(body).__name__), \
                    patch.object(legacy, 'api_request', return_value=(status, body)) as api, \
                    patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                with self.assertRaisesRegex(RuntimeError, 'cannot establish complete'):
                    legacy.open_pull_request('fixture-token', 'fixture-repo', 'proofread/fixture-config')
                self.assertEqual(api.call_count, 1)
                self.assertEqual(api.call_args.args[1], 'GET')

    def test_any_prior_pr_including_closed_and_malformed_stops_for_review(self):
        for entry in ({'number': 1, 'state': 'open'}, {'number': 2, 'state': 'closed'}, None, {}):
            with self.subTest(entry=entry), patch.object(legacy, 'api_request', return_value=(200, [entry])):
                with self.assertRaisesRegex(RuntimeError, 'separate recovery review'):
                    legacy.open_pull_request('fixture-token', 'fixture-repo', 'proofread/fixture-config')

    def test_only_a_complete_empty_pr_response_returns_absent(self):
        with patch.object(legacy, 'api_request', return_value=(200, [])) as api:
            self.assertIsNone(legacy.open_pull_request('fixture-token', 'fixture-repo', 'proofread/fixture-config'))
        self.assertIn('state=all', api.call_args.args[2])

    def test_existing_branch_without_pr_stops_before_any_content_read_or_write(self):
        for mode in ('single', 'batch'):
            with self.subTest(mode=mode), \
                    patch.object(legacy, 'api_request', return_value=(200, [])), \
                    patch.object(legacy, 'branch_sha', return_value='a' * 40), \
                    patch.object(legacy, 'get_file') as read_file, \
                    patch.object(legacy, 'response_or_fail') as writer, \
                    patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                with self.assertRaisesRegex(RuntimeError, 'no verified PR'):
                    if mode == 'single':
                        legacy.submit_file('fixture-token', 'fixture-repo', 'config', 'fixture.ts',
                                           'requested-content', 'title', 'description', 'fixture')
                    else:
                        legacy.submit_files('fixture-token', 'fixture-repo', 'config',
                                            {'fixture.ts': 'requested-content'}, 'title', 'description', 'fixture')
                read_file.assert_not_called()
                writer.assert_not_called()

    def test_malformed_ref_response_is_not_absent(self):
        for status, body in ((403, {}), (200, {}), (200, {'object': {}}),
                             (200, {'object': {'sha': ''}}), (200, {'object': {'sha': 'f' * 39}})):
            with self.subTest(status=status, body=body), patch.object(legacy, 'api_request', return_value=(status, body)):
                with self.assertRaises(RuntimeError):
                    legacy.branch_sha('fixture-token', 'fixture-repo', 'proofread/fixture-config')
        with patch.object(legacy, 'api_request', return_value=(404, {})):
            self.assertIsNone(legacy.branch_sha('fixture-token', 'fixture-repo', 'proofread/fixture-config'))
        with patch.object(legacy, 'api_request', return_value=(200, {'object': {'sha': 'a' * 40}})):
            self.assertEqual(legacy.branch_sha('fixture-token', 'fixture-repo', 'proofread/fixture-config'), 'a' * 40)

    def test_missing_or_malformed_base_stops_before_branch_creation(self):
        for mode in ('single', 'batch'):
            for base_response in ((404, {}), (403, {}), (200, {}), (200, {'object': {'sha': ''}})):
                with self.subTest(mode=mode, status=base_response[0]), \
                        patch.object(legacy, 'api_request', side_effect=[(200, []), (404, {}), base_response]), \
                        patch.object(legacy, 'get_file', return_value=('', None)), \
                        patch.object(legacy, 'response_or_fail') as writer:
                    with self.assertRaises(RuntimeError):
                        if mode == 'single':
                            legacy.submit_file('fixture-token', 'fixture-repo', 'config', 'fixture.ts',
                                               'requested', 'title', 'description', 'fixture')
                        else:
                            legacy.submit_files('fixture-token', 'fixture-repo', 'config',
                                                {'fixture.ts': 'requested'}, 'title', 'description', 'fixture')
                    writer.assert_not_called()

    def test_new_synthetic_submissions_still_create_the_expected_effects(self):
        for mode in ('single', 'batch'):
            with self.subTest(mode=mode):
                calls = []

                def read(_token, _repo, _branch, _path):
                    return '', None

                def send(_token, method, path, _expected, payload=None):
                    calls.append((method, path, payload))
                    if method == 'POST' and path.endswith('/pulls'):
                        return {'number': 1, 'html_url': 'https://example.invalid/pr/1',
                                'head': {'sha': 'b' * 40, 'ref': 'proofread/fixture-config'},
                                'base': {'ref': 'config'}}
                    return {}

                with patch.object(legacy, 'api_request', side_effect=[(200, []), (404, {}),
                                                                      (200, {'object': {'sha': 'a' * 40}})]), \
                        patch.object(legacy, 'get_file', side_effect=read), \
                        patch.object(legacy, 'response_or_fail', side_effect=send), \
                        patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                    if mode == 'single':
                        result = legacy.submit_file('fixture-token', 'fixture-repo', 'config', 'fixture.ts',
                                                    'requested-content', 'title', 'description', 'fixture')
                    else:
                        result = legacy.submit_files('fixture-token', 'fixture-repo', 'config',
                                                     {'fixture.ts': 'requested-content'}, 'title', 'description', 'fixture')
                self.assertEqual([method for method, _path, _payload in calls], ['POST', 'PUT', 'POST'])
                self.assertEqual(result['number'], 1)


if __name__ == '__main__':
    unittest.main()
