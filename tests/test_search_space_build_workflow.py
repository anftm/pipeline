"""Search image validation must build a pinned current-branch Space commit."""
import unittest
from pathlib import Path

import yaml


class SearchSpaceBuildWorkflowTests(unittest.TestCase):
    def test_build_is_manual_pinned_and_checks_selected_generation(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-space-build-check.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        steps = {step.get('name'): step for step in flow['jobs']['build']['steps'] if step.get('name')}
        checkout = steps['Fetch exact public Space commit and LFS objects']['run']
        self.assertIn('--single-branch --branch main --no-checkout', checkout)
        self.assertIn('checkout --detach "$HF_REVISION"', checkout)
        self.assertIn('lfs fsck', checkout)
        build = steps['Build Docker image with the requested immutable generation']['run']
        self.assertIn('SEARCH_DATA_GENERATION=$SEARCH_GENERATION', build)
        acceptance = steps['Start image and verify actual generation and searches']['run']
        self.assertIn("data.get('verified') is True", acceptance)
        self.assertIn("data.get('generation') == os.environ['SEARCH_GENERATION']", acceptance)
        self.assertIn('/api/search', acceptance)


if __name__ == '__main__': unittest.main()
