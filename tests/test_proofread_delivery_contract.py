"""Lock the safety assumptions that keep proofreading submissions paused."""
from pathlib import Path
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

    def test_delivery_design_explicitly_disallows_activation_and_imagined_replay(self):
        text = (ROOT / 'scripts/proofread_delivery_design.txt').read_text()
        for phrase in ('not an activation procedure', 'Do not invent or replay missing events',
                       'GitHub repository_dispatch response of 204', 'partial outcome',
                       'confer no permission to enable Actions'):
            self.assertIn(phrase, text)


if __name__ == '__main__':
    unittest.main()
