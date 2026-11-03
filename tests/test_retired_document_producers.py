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
            'reader-ocr-audit.yml': {'audit'},
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
        self.assertIn('auto|build-stage|stage|promote|rollback|correct|accept|reject|retry|repair-gbk|retire-ocr-progress)', workflow)
        self.assertIn('Retired document OCR/text operation is disabled', workflow)
        self.assertIn('unify|reset-documents)', workflow)

    def test_v3_keeps_only_the_existing_daily_retirement_schedule(self):
        workflow = yaml.safe_load((ROOT / 'reader-v3-publish.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual(triggers['schedule'], [{'cron': '23 5 * * *'}])
        self.assertIn('workflow_dispatch', triggers)
        self.assertIn('unify', triggers['workflow_dispatch']['inputs']['operation']['options'])

    def test_v3_no_longer_records_ocr_progress_or_receives_correction_secrets(self):
        workflow = yaml.safe_load((ROOT / 'reader-v3-publish.yml').read_text())
        steps = workflow['jobs']['publish']['steps']
        self.assertFalse(any('pdf_progress_report.py' in step.get('run', '') for step in steps))
        operation = next(step for step in steps if step.get('id') == 'operation')
        self.assertNotIn('OCR_CORRECTION_API_BASE', operation['env'])
        self.assertNotIn('OCR_CORRECTION_API_KEY', operation['env'])

    def test_retired_v3_cli_operations_fail_before_creating_store(self):
        from unittest.mock import patch
        from scripts import publish_reader_v3
        operations = ('auto', 'build-task', 'build-stage', 'stage', 'promote', 'rollback',
                      'correct', 'accept', 'reject', 'retry', 'repair-gbk', 'retire-ocr-progress')
        for operation in operations:
            with self.subTest(operation=operation), \
                 patch('sys.argv', ['publish_reader_v3.py', operation, '--apply']), \
                 patch.object(publish_reader_v3, 'CentralHubStore', side_effect=AssertionError('no remote access')):
                with self.assertRaisesRegex(RuntimeError, 'remote OCR/detached-text v3 operation is retired'):
                    publish_reader_v3.main()

    def test_retired_ccrd_audit_and_watchdog_have_no_automatic_triggers(self):
        for name in ('publish-ccrd-ocr.yml', 'reader-ocr-audit.yml', 'pdf-lane-watchdog.yml'):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((ROOT / name).read_text())
                triggers = workflow.get('on', workflow.get(True))
                self.assertEqual(set(triggers), {'workflow_dispatch'})
        from scripts.reader_gc_fence import producer_path
        self.assertTrue(producer_path('.github/workflows/pdf-lane-watchdog.yml'))

    def test_current_and_historical_ocr_audit_names_are_both_fenced(self):
        from scripts.reader_document_reset import DOCUMENT_PRODUCERS
        self.assertIn('reader-ocr-audit.yml', DOCUMENT_PRODUCERS)
        self.assertIn('reader-ocr-language-audit.yml', DOCUMENT_PRODUCERS)

    def test_remote_normalized_text_publisher_is_not_scheduled_or_runnable(self):
        workflow = yaml.safe_load((ROOT / 'reader-publish-text.yml').read_text())
        self.assertEqual(set(workflow.get('on', workflow.get(True))), {'workflow_dispatch'})
        self.assertEqual(workflow['jobs']['publish-source-txt']['if'], '${{ false }}')
        from scripts.reader_gc_fence import producer_path
        self.assertTrue(producer_path('.github/workflows/reader-publish-text.yml'))

    def test_chm_conversion_is_local_and_ps_reader_conversion_is_not_removed(self):
        chm = (ROOT / 'reader-chm-repair.yml').read_text()
        static = (ROOT / 'reader-static-pdf-stream.yml').read_text()
        self.assertIn('EPUB is temporary', chm)
        self.assertIn('options: [all, ppt, pptx, pps, wps, ps, tif, tiff]', static)


if __name__ == '__main__':
    unittest.main()
