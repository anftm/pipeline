from pathlib import Path
import unittest

import yaml


class CentralEbookWorkflowTests(unittest.TestCase):
    def test_serial_default_off_writer_and_readonly_prepare_are_separate(self):
        path = Path(__file__).resolve().parents[1]/'.github/workflows/reader-v3-publish.yml'
        workflow = yaml.safe_load(path.read_bytes())
        self.assertEqual(workflow['concurrency']['group'], 'reader-sidecar')
        job = workflow['jobs']['publish']
        self.assertEqual(job['concurrency']['group'], 'daily-sync')
        entry = next(s for s in job['steps'] if s.get('name') == 'Prepare or publish exact same-source EPUB candidates')
        run = entry['run']
        self.assertIn('ls-remote --exit-code https://github.com/anftm/pipeline.git refs/heads/main', run)
        self.assertIn('[[ "$main" == "$GITHUB_SHA"$\'\\trefs/heads/main\' ]]', run)
        self.assertNotIn('gh api', run)
        preview, recovery = run.split('elif [[ "$OPERATION" == recover-exact-ebook ]]; then', 1)
        recovery, apply = recovery.rsplit('\nelse\n', 1)
        self.assertIn('[[ "$APPLY" == false && "$RECOVERY_PHASE" == inspect ]]', preview)
        self.assertIn('reader_ebook_rebuild_publish --recover "$RECOVERY_PHASE"', recovery)
        self.assertIn('if [[ "$APPLY" == false ]]; then observe+=(--observe-only); fi', recovery)
        self.assertIn('[[ "$RECOVERY_PHASE" == pages || "$APPLY" == true ]]', recovery)
        self.assertNotIn('reader_ebook_rebuild_publish --apply', recovery)
        self.assertNotIn('--apply', preview)
        self.assertIn('READER_EBOOK_PUBLISH_ENABLED" == true', apply)
        self.assertIn('READER_WEB_PAGES_DISPATCH_SCOPE_REVIEWED" == true', apply)
        self.assertLess(apply.index('--preflight'), apply.index('reader_ebook_rebuild_publish --apply'))
        self.assertLess(apply.index('reader_ebook_rebuild_publish --apply'), apply.index('verify-generation'))
        main = next(s for s in job['steps'] if s.get('id') == 'operation')
        for mode in ('prepare-exact-ebook','replace-exact-ebook','recover-exact-ebook'):
            self.assertIn(f"inputs.operation != '{mode}'", main['if'])
