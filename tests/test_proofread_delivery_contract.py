"""Lock the safety assumptions that keep proofreading submissions paused."""
from pathlib import Path
import os
import unittest
from unittest.mock import patch

import yaml

from scripts import submit_proofread


ROOT = Path(__file__).resolve().parents[1]


class ProofreadDeliveryContractTests(unittest.TestCase):
    def test_dispatch_workflows_retain_the_expected_event_types(self):
        for filename, event in (('proofread-submit.yml', 'submit-proofread'),
                                ('proofread-submit-parse.yml', 'submit-parse')):
            workflow = yaml.load((ROOT / '.github/workflows' / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(workflow['on']['repository_dispatch']['types'], [event])
            self.assertTrue(any(step.get('run') == 'python3 scripts/submit_proofread.py'
                                for job in workflow['jobs'].values() for step in job.get('steps', [])))

    def test_invalid_segmented_request_is_rejected_before_github_blob_read(self):
        invalid = {'payload_blobs': ['not-a-sha'], 'payload_sha256': '0' * 64,
                   'payload_characters': 1, 'request_id': 'fixture'}
        with patch.object(submit_proofread, 'response_or_fail', side_effect=AssertionError('network forbidden')):
            with self.assertRaises(RuntimeError):
                submit_proofread.segmented_request(invalid)

    def test_no_event_or_partial_replay_is_processed_without_durable_delivery(self):
        payload = {'kind': 'proofread', 'request_id': 'uncertain-existing-request',
                   'payload_blobs': ['a' * 40], 'payload_sha256': 'b' * 64,
                   'payload_characters': 1}
        for environment in ({}, {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0', 'GH_PAT': 'fixture-token'}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                    patch.object(submit_proofread, 'load_request', return_value=payload) as load, \
                    patch.object(submit_proofread, 'api_request') as api, \
                    patch.object(submit_proofread, 'response_or_fail') as write, \
                    patch('urllib.request.urlopen') as network:
                with self.assertRaisesRegex(RuntimeError, 'delivery is paused'):
                    submit_proofread.main()
                load.assert_not_called()
                api.assert_not_called()
                write.assert_not_called()
                network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
