"""Retired OCR workflows must not regenerate remote document text or inputs."""
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1] / '.github/workflows'
RETIRED_WORKFLOWS = {
    'exact-stale-run-delete.yml', 'force-ocr-book.yml', 'pdf-lane-watchdog.yml',
    'publish-ccrd-ocr.yml', 'reader-ocr-audit.yml', 'reader-ocr-repin.yml',
    'reader-publish-text.yml', 'reader-v3-build.yml', 'stage-ccrd-ocr-text.yml',
    'pdf-account-publish.yml',
}


class RetiredDocumentProducerTests(unittest.TestCase):
    def test_retired_entrypoints_are_absent_and_not_dispatched(self):
        for filename in RETIRED_WORKFLOWS:
            with self.subTest(filename=filename):
                self.assertFalse((ROOT / filename).exists())
                for workflow in ROOT.glob('*.yml'):
                    self.assertNotIn(filename, workflow.read_text(), workflow.name)

    def test_historical_account_publisher_remains_a_fenced_document_producer(self):
        from scripts.reader_document_reset import DOCUMENT_PRODUCERS
        from scripts.reader_gc_fence import producer_path
        self.assertIn('pdf-account-publish.yml', DOCUMENT_PRODUCERS)
        self.assertTrue(producer_path('.github/workflows/pdf-account-publish.yml'))

    def test_no_workflow_runs_retired_standalone_remote_publishers(self):
        scripts = ('force_ocr_book.py', 'audit_ocr_language.py', 'repin_ocr_pages.py',
                   'publish_ccrd_ocr.py', 'auto_ccrd_ocr.py', 'export_reader_txt.py',
                   'build_ccrd_ocr_release.py', 'pilot_text_stream.py', 'exact_stale_run_delete.py',
                   'pdf_ocr.py', 'pdf_ocr_stages.py', 'plan_pdf_ocr.py', 'run_pdf_ocr.py',
                   'publish_pdf_ocr_assets.py', 'publish_lin_native_text.py',
                   'report_ocr_patch_conflicts.py', 'review_ocr_patch_conflicts.py')
        for path in ROOT.glob('*.yml'):
            workflow = yaml.safe_load(path.read_text())
            for name, job in workflow['jobs'].items():
                for step in job.get('steps', []):
                    run = step.get('run', '')
                    for script in scripts:
                        with self.subTest(workflow=path.name, job=name, script=script):
                            self.assertNotIn('scripts/' + script, run)

    def test_worker_is_an_inert_inventory_sentinel(self):
        workflow = yaml.safe_load((ROOT / 'pdf-account-worker.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual(set(triggers), {'workflow_dispatch'})
        self.assertEqual(workflow['permissions'], {})
        self.assertEqual(set(workflow['jobs']), {'retired'})
        job = workflow['jobs']['retired']
        self.assertEqual(job['if'], '${{ false }}')
        self.assertEqual(job['steps'], [{'name': 'Retired', 'run': 'exit 1'}])
        self.assertNotIn('env', workflow)
        self.assertNotIn('env', job)

    def test_ocr_review_and_its_dispatch_are_removed(self):
        self.assertFalse((ROOT / 'proofread-ocr-conflict-review.yml').exists())
        workflow = yaml.safe_load((ROOT / 'bha-update-index.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertNotIn('repository_dispatch', triggers)
        self.assertNotIn('allow_ocr_patch_rebase', triggers['workflow_dispatch']['inputs'])
        self.assertNotIn('ALLOW_OCR_PATCH_REBASE', (ROOT / 'bha-update-index.yml').read_text())
        requirements = ROOT.parents[1] / 'scripts/requirements-pdf-ocr.txt'
        self.assertFalse(requirements.exists())

    def test_recognition_implementations_and_model_dependencies_are_removed(self):
        scripts = ROOT.parents[1] / 'scripts'
        for name in ('pdf_ocr.py', 'pdf_ocr_stages.py', 'plan_pdf_ocr.py', 'run_pdf_ocr.py',
                     'publish_pdf_ocr_assets.py', 'publish_ccrd_ocr.py', 'ocr_quality.py',
                     'ndl_ocr.py', 'auto_ccrd_ocr.py', 'repin_ocr_pages.py'):
            with self.subTest(script=name):
                self.assertFalse((scripts / name).exists())
        converter = (scripts / 'convert_reader_assets.py').read_text()
        self.assertNotIn('tesseract', converter.lower())
        self.assertNotIn('rapidocr', converter.lower())
        self.assertIn('page_content_ratio', converter)

    def test_pdf_djvu_page_stream_builders_and_publisher_are_removed(self):
        scripts = ROOT.parents[1] / 'scripts'
        for name in ('pdf_assets.py', 'migrate_pdf_page_manifests.py'):
            with self.subTest(script=name):
                self.assertFalse((scripts / name).exists())
        from scripts import djvu_pages, reader_page_codec
        for name in ('render', 'render_page', 'render_profile'):
            with self.subTest(entry=name):
                self.assertFalse(hasattr(djvu_pages, name))
        for name in ('encode_png', 'encode_image', '_encode'):
            with self.subTest(entry=name):
                self.assertFalse(hasattr(reader_page_codec, name))
        for workflow in ROOT.glob('*.yml'):
            self.assertNotIn('scripts/pdf_assets.py', workflow.read_text(), workflow.name)
            self.assertNotIn('scripts/migrate_pdf_page_manifests.py', workflow.read_text(), workflow.name)

    def test_v3_unify_and_status_remain_available_but_old_text_operations_stop(self):
        workflow = (ROOT / 'reader-v3-publish.yml').read_text()
        self.assertIn('unify|reset-documents)', workflow)
        config = yaml.safe_load(workflow)
        inputs = config.get('on', config.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(inputs['operation']['options'],
                         ['inspect', 'unify', 'ack', 'retire', 'withdraw', 'reset-documents',
                          'remove-route', 'restore-route', 'verify-route', 'migrate-derived', 'resume-derived',
                          'redirect-derived', 'publish-generation', 'verify-generation'])
        self.assertEqual(inputs['operation']['default'], 'inspect')
        self.assertFalse(inputs['apply']['default'])

    def test_v3_does_not_automatically_run_retirement_on_schedule(self):
        workflow = yaml.safe_load((ROOT / 'reader-v3-publish.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertNotIn('schedule', triggers)
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
                with self.assertRaises(SystemExit):
                    publish_reader_v3.main()

    def test_historical_paths_remain_fenced_after_entrypoint_removal(self):
        from scripts.reader_gc_fence import producer_path
        for name in RETIRED_WORKFLOWS - {'exact-stale-run-delete.yml'}:
            with self.subTest(workflow=name):
                self.assertTrue(producer_path('.github/workflows/' + name))

    def test_current_and_historical_ocr_audit_names_are_both_fenced(self):
        from scripts.reader_document_reset import DOCUMENT_PRODUCERS
        self.assertIn('reader-ocr-audit.yml', DOCUMENT_PRODUCERS)
        self.assertIn('reader-ocr-language-audit.yml', DOCUMENT_PRODUCERS)

    def test_remote_normalized_text_publisher_is_not_scheduled_or_runnable(self):
        self.assertFalse((ROOT / 'reader-publish-text.yml').exists())
        from scripts.reader_gc_fence import producer_path
        self.assertTrue(producer_path('.github/workflows/reader-publish-text.yml'))

    def test_chm_conversion_is_local_and_ps_reader_conversion_is_not_removed(self):
        chm = (ROOT / 'reader-chm-repair.yml').read_text()
        static = (ROOT / 'reader-static-pdf-stream.yml').read_text()
        self.assertIn('EPUB is temporary', chm)
        self.assertIn('options: [all, ppt, pptx, pps, wps, ps, tif, tiff]', static)

    def test_normal_reader_publish_calls_still_target_unify(self):
        callers = ('reader-chm-repair.yml', 'reader-ebook-stream.yml', 'reader-office-chapters.yml',
                   'reader-web-stream.yml', 'reader-static-pdf-stream.yml', 'reader-spreadsheet-stream.yml',
                   'reader-image-stream.yml', 'reader-media-stream.yml',
                   'reader-chapter-search-repair.yml')
        for name in callers:
            with self.subTest(workflow=name):
                text = (ROOT / name).read_text()
                self.assertIn('gh workflow run reader-v3-publish.yml', text)
                self.assertIn('operation=unify', text)


if __name__ == '__main__':
    unittest.main()
