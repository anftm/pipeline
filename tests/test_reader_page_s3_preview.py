"""Read-only S3 preflight is opt-in and never weakens the migration fence."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import publish_reader_v3 as publication
from scripts import reader_page_migrate as migration
from scripts.reader_page_cas import ConditionalPageStore


IDENTITIES = {'index_sha256': 'a' * 64, 'sidecar_sha256': 'b' * 64, 'v3_generation': 'c' * 64}
PLAN = {'audit': {'planned': [1], 'unindexed_legacy_pdfs': [], 'unclassified_object_pdfs': []},
        'changed_routes': 1, 'generation': 'd' * 32, 'pointer': {'generation': 'c' * 64}}


class PagePreviewTests(unittest.TestCase):
    def invoke(self, options, *, apply=False):
        primary = Mock()
        args = ['publish_reader_v3.py', 'migrate-derived', '--resource-json', json.dumps(options)]
        if apply:
            args.append('--apply')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            args += ['--output', str(output)]
            with patch.object(sys, 'argv', args), patch.object(publication, 'CentralHubStore', return_value=primary), \
                    patch.object(migration, 'require_writer_window') as fence, \
                    patch.object(migration, 'prepare', return_value=PLAN), \
                    patch.object(migration, 'apply', return_value={'applied': True}) as mutate, \
                    patch.object(ConditionalPageStore, '__init__', return_value=None) as init, \
                    patch.object(ConditionalPageStore, 'preflight') as preflight:
                publication.main()
                report = json.loads(output.read_text())
                return report, fence.call_count, mutate.call_count, init.call_count, preflight.call_count

    def test_opted_in_preview_checks_s3_roots_but_never_fences_or_applies(self):
        report, fence, mutate, init, preflight = self.invoke({**IDENTITIES, 'check_s3': True})
        self.assertFalse(report['applied'])
        self.assertTrue(report['s3_roots_verified'])
        self.assertFalse(report['objects_deleted'])
        self.assertEqual((fence, mutate, init, preflight), (0, 0, 1, 1))

    def test_existing_preview_without_s3_is_unchanged(self):
        report, fence, mutate, init, preflight = self.invoke(IDENTITIES)
        self.assertFalse(report['applied'])
        self.assertNotIn('s3_roots_verified', report)
        self.assertEqual((fence, mutate, init, preflight), (0, 0, 0, 0))

    def test_apply_always_checks_the_fence_and_preflight(self):
        report, fence, mutate, init, preflight = self.invoke(
            {**IDENTITIES, 'external_writers_stopped': True}, apply=True)
        self.assertTrue(report['applied'])
        self.assertEqual((fence, mutate, init, preflight), (1, 1, 1, 1))

    def test_preview_only_flag_cannot_change_apply_contract(self):
        for options, apply in [({**IDENTITIES, 'check_s3': False}, False),
                               ({**IDENTITIES, 'check_s3': 'true'}, False),
                               ({**IDENTITIES, 'external_writers_stopped': True, 'check_s3': True}, True),
                               ({**IDENTITIES, 'external_writers_stopped': True}, False)]:
            with self.subTest(options=options, apply=apply):
                with self.assertRaisesRegex(ValueError, 'migrate-derived requires'):
                    self.invoke(options, apply=apply)


if __name__ == '__main__':
    unittest.main()
