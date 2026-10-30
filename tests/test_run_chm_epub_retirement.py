import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from scripts import run_chm_epub_retirement as runner
from scripts import chm_epub_publication_safety as safety


class RunChmRetirementTests(unittest.TestCase):
    def test_exact_store_cannot_delete_any_foreign_bucket_or_key(self):
        row={'bucket':'vomebook/reader-assets-v2','path':'native/ebook/chm/reviewed/document.epub'}
        store=runner.ExactStore.__new__(runner.ExactStore);store.allowed={(row['bucket'],row['path'])}
        with patch.object(runner.S3BucketStore,'delete') as deleted:
            for bucket,paths in [('vomebook/pdf-pages-v2',[row['path']]),
                                 (row['bucket'],['documents/pdf/ps/book.pdf']),
                                 (row['bucket'],[row['path'],row['path']])]:
                with self.assertRaisesRegex(ValueError,'exceeds'):
                    store.delete(bucket,paths)
            deleted.assert_not_called()
            store.delete(row['bucket'],[row['path']])
            deleted.assert_called_once_with(store,row['bucket'],[row['path']])

    def test_pages_dispatch_requires_exact_repo_pointer_and_fence(self):
        pointer={'generation':'reviewed'}
        plan={'overrides':{runner.publication.POINTER:json.dumps(pointer).encode()}}
        fence=Mock()
        with patch.dict('os.environ',{'PAGES_REPO':'vomebook/search','PAGES_TOKEN':'token'}), \
             patch.object(runner.publication,'current',return_value=(pointer,{})), \
             patch.object(runner.httpx,'post') as post:
            post.return_value.raise_for_status=Mock()
            runner.request_pages_deploy(object(),fence,plan)
            fence.check.assert_called_once()
            self.assertIn('/repos/vomebook/search/actions/workflows/static.yml/dispatches',post.call_args.args[0])
        with patch.dict('os.environ',{'PAGES_REPO':'foreign/repo','PAGES_TOKEN':'token'}), \
             patch.object(runner.httpx,'post') as post:
            with self.assertRaisesRegex(ValueError,'unexpected Pages'):
                runner.request_pages_deploy(object(),fence,plan)
            post.assert_not_called()

    def test_workflow_serializes_and_exposes_explicit_resume(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/chm-epub-retirement.yml'
        workflow=yaml.safe_load(path.read_text())
        self.assertEqual(workflow['concurrency']['group'],'publish-ccrd-index')
        job=workflow['jobs']['retire']
        self.assertEqual(job['concurrency']['group'],'reader-global-maintenance')
        self.assertIn("inputs.confirm == 'RETIRE-972'",job['if'])
        trigger=workflow.get('on',workflow.get(True))['workflow_dispatch']['inputs']
        self.assertIn('resume',trigger)
        self.assertIn('tests.test_chm_epub_journal',next(s['run'] for s in job['steps'] if 'Test exact' in s.get('name','')))


if __name__=='__main__':
    unittest.main()
