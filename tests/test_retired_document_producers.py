"""Retired OCR workflows must not regenerate remote document text or inputs."""
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1] / '.github/workflows'


class RetiredDocumentProducerTests(unittest.TestCase):
    def test_ocr_jobs_are_disabled_even_for_manual_dispatch(self):
        jobs = {
            'pdf-lane-watchdog.yml': {'watchdog'},
            'pdf-account-publish.yml': {'publish'},
            'force-ocr-book.yml': {'prepare', 'publish'},
            'stage-ccrd-ocr-text.yml': {'stage'},
            'reader-ocr-repin.yml': {'repin'},
            'publish-ccrd-ocr.yml': {'publish', 'automatic', 'newspapers'},
            'reader-v3-build.yml': {'build', 'promote'},
        }
        for filename, names in jobs.items():
            with self.subTest(filename=filename):
                workflow = yaml.safe_load((ROOT / filename).read_text())
                self.assertEqual(set(workflow['jobs']), names)
                for job in workflow['jobs'].values():
                    self.assertEqual(job.get('if'), '${{ false }}')

    def test_worker_plan_cannot_dispatch_ocr_or_publish_artifacts(self):
        workflow = yaml.safe_load((ROOT / 'pdf-account-worker.yml').read_text())
        self.assertEqual(workflow['jobs']['plan']['if'], '${{ false }}')
        self.assertIn('plan', workflow['jobs']['build']['needs'])
        self.assertIn('plan', workflow['jobs']['notify']['needs'])

    def test_v3_unify_and_status_remain_available_but_old_text_operations_stop(self):
        workflow = (ROOT / 'reader-v3-publish.yml').read_text()
        self.assertIn('auto|build-stage|stage|promote|rollback|correct|accept|reject|retry|repair-gbk)', workflow)
        self.assertIn('Retired document OCR/text operation is disabled', workflow)
        self.assertIn('unify|reset-documents)', workflow)

    def test_chm_conversion_is_local_and_ps_reader_conversion_is_not_removed(self):
        chm = (ROOT / 'reader-chm-repair.yml').read_text()
        static = (ROOT / 'reader-static-pdf-stream.yml').read_text()
        self.assertIn('EPUB is temporary', chm)
        self.assertIn('options: [all, ppt, pptx, pps, wps, ps, tif, tiff]', static)


if __name__ == '__main__':
    unittest.main()
