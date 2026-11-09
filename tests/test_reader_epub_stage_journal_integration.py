"""Journal, single-object adapters and EPUB staging as one isolated protocol."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import reader_epub_immutable,reader_epub_protection,reader_epub_shadow,reader_epub_stage_delivery,reader_epub_stage_journal
from scripts.reader_bucket_store import S3BucketStore
from tests.test_reader_epub_shadow import epub,plan
from tests.test_reader_epub_stage_integration import AtomicFakeS3,WHEN


class JournalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'shadow';raw=epub();self.plan=plan(raw)
        self.report=reader_epub_shadow.build(self.plan,self.root,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=reader_epub_shadow.staging_plan(self.plan,self.report,self.root)
        self.client=AtomicFakeS3();self.client.n=0
        original_put=self.client.put_object
        def versioned_put(**kwargs):
            # AtomicFakeS3 only needs an initial conditional head here. Keep
            # different strong validators for updates and require exact match.
            if 'IfMatch' in kwargs:
                expected=kwargs.pop('IfMatch');key=kwargs['Key']
                self.client.calls.append(('put',key))
                actual=self.client.values.get(key)
                if actual is None or expected!='"'+hashlib.sha256(actual).hexdigest()+'"':
                    from tests.test_reader_epub_stage_integration import failure
                    raise failure('PreconditionFailed',412)
                raw=kwargs['Body'] if isinstance(kwargs['Body'],bytes) else kwargs['Body'].read()
                self.client.values[key]=raw
                return {'ETag':'"'+hashlib.sha256(raw).hexdigest()+'"'}
            return original_put(**kwargs)
        self.client.put_object=versioned_put
        original_get=self.client.get_object
        def versioned_get(**kwargs):
            result=original_get(**kwargs)
            content=self.client.values.get(kwargs['Key'])
            result['ETag']='"'+hashlib.sha256(content).hexdigest()+'"'
            return result
        self.client.get_object=versioned_get
        raw_store=S3BucketStore.__new__(S3BucketStore)
        raw_store._location=Mock(return_value=('vomebook','reader-assets-v2'))
        raw_store._client=Mock(return_value=self.client)
        self.journal=reader_epub_stage_journal.S3Journal(raw_store)
        self.protections=reader_epub_protection.S3EpubProtections(raw_store)
        self.objects=reader_epub_immutable.S3EpubObjects(raw_store)
        self.validate=Mock()
    def prepare(self):
        return reader_epub_stage_delivery.prepare(self.journal,self.plan,self.report,self.root,
                                                  created_at=WHEN,revalidate=self.validate)
    def apply(self,current):
        return reader_epub_stage_delivery.apply_once(self.journal,current,self.report,self.root,
            protections=self.protections,objects=self.objects,revalidate=self.validate)
    def recover(self):
        return reader_epub_stage_delivery.recover(self.journal,self.plan['operation'],
            protections=self.protections,objects=self.objects,revalidate=self.validate)
    def test_all_steps_are_conditional_and_staged_is_not_published(self):
        first=self.prepare();self.assertEqual(first.record['state'],'prepared')
        staged=self.apply(first);self.assertEqual(staged.record['state'],'staged')
        self.assertEqual(reader_epub_stage_journal.load(self.journal,self.plan['operation']),staged)
        names=[key for kind,key in self.client.calls if kind=='put']
        self.assertFalse(any(key=='reader-index/v3/current.json' for key in names))
        self.assertFalse(any(key.endswith('/index.json') for key in names))
        self.assertEqual(sum(key.startswith('reader-index/processing/epub-exact-') for key in names),1)
        self.assertEqual(sum(key.startswith(self.preview['actions'][0]['root']+'/') for key in names),
                         len(self.preview['actions'][0]['files']))
        with self.assertRaisesRegex(ValueError,'not prepared'):self.apply(staged)
    def test_interrupt_after_complete_stage_recovers_without_any_object_replay(self):
        first=self.prepare();calls=0
        def validation(_plan,_preview):
            nonlocal calls
            calls+=1
            if calls==4:raise TimeoutError('after objects before receipt')
        self.validate.side_effect=validation
        with self.assertRaises(TimeoutError):self.apply(first)
        self.validate.side_effect=None
        before=[key for kind,key in self.client.calls if kind=='put' and key.startswith('chapters/')]
        pending=reader_epub_stage_journal.load(self.journal,self.plan['operation'])
        self.assertEqual(pending.record['state'],'write-intent')
        done=self.recover();after=[key for kind,key in self.client.calls if kind=='put' and key.startswith('chapters/')]
        self.assertEqual(before,after);self.assertEqual(done.record['state'],'staged')
        self.assertFalse(any(key.endswith('/index.json') for kind,key in self.client.calls if kind=='put'))
    def test_partial_failure_remains_pending_without_cleanup_or_retry(self):
        first=self.prepare();first_file=self.preview['actions'][0]['files'][0]['remote'];self.client.before_once.add(first_file)
        with self.assertRaises(Exception):self.apply(first)
        with self.assertRaisesRegex(ValueError,'missing or changed'):self.recover()
        self.assertEqual(self.client.calls.count(('put',first_file)),1)
        self.assertEqual(reader_epub_stage_journal.load(self.journal,self.plan['operation']).record['state'],'write-intent')
        self.assertIn(self.preview['actions'][0]['protection_path'],self.client.values)


if __name__=='__main__':unittest.main()
