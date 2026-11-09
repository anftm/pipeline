"""EPUB index preview modifies only selected rows and never writes a Reader root."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import reader_epub_index_preview as subject,reader_epub_shadow as shadow
from scripts import reader_epub_stage_journal as journal,search_snapshot
from tests.test_reader_epub_shadow import epub,plan
from tests.test_reader_epub_stage_journal import MemoryJournal,WHEN
from tests.test_reader_epub_protected_stage import MemoryObjects,MemoryProtections


class IndexPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'shadow';raw=epub();self.plan=plan(raw)
        source=self.plan['sources'][0];key=source['key']
        self.old={'key':key,'repo':source['repo'],'path':source['path'],'extension':'epub','source_extension':'epub',
                  'source_revision':source['previous_revision'],'source_sha256':source['previous_source_sha256'],
                  'manifest':source['previous_manifest'],'bucket':'vomebook/reader-assets-v2','profile':'old'}
        self.other={'key':'VoiceOfML/Test\0other.docx','source_revision':'x','manifest':'preserve'}
        self.canonical={'version':1,'kind':'ebook-chapter-stream-index','files':[self.old,self.other],'failures':[]}
        self.shard={'version':1,'kind':'ebook-chapter-stream-index','files':[self.old],'failures':[]}
        self.plan['canonical_index_sha256']=hashlib.sha256(search_snapshot.canonical(self.canonical)).hexdigest()
        self.plan['shard_index_sha256']=hashlib.sha256(search_snapshot.canonical(self.shard)).hexdigest()
        self.plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.plan.items() if k!='plan_sha256'})).hexdigest()
        self.report=shadow.build(self.plan,self.root,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.root)
        self.store=MemoryJournal();prepared=journal.begin(self.store,self.plan,self.preview,WHEN);claimed=journal.claim(self.store,prepared)
        action=self.preview['actions'][0];outcomes=[{'kind':'protection','path':action['protection_path'],'status':'written'}]
        outcomes += [{'kind':'object','path':x['remote'],'status':'written'} for x in action['files']]
        receipt={'version':1,'kind':'reader-epub-protected-staging','operation':self.plan['operation'],
                 'plan_sha256':self.plan['plan_sha256'],'staging_sha256':self.preview['staging_sha256'],
                 'protected_sources':1,'verified_objects':len(action['files']),'outcomes':outcomes,
                 'index_written':False,'pointer_written':False,'publication_ready':False,'external_writers_verified':False}
        self.current=journal.record_staged(self.store,claimed,receipt)
        self.events=[];self.protections=MemoryProtections(self.events);self.objects=MemoryObjects(self.events)
        from scripts import reader_epub_protection as p
        self.protections.values[action['protection_path']]=search_snapshot.canonical(
            p.make_record(self.plan['operation'],self.plan['plan_sha256'],action['root'],WHEN))
        for x in action['files']:self.objects.values[x['remote']]=(self.root/'0'/'bundle'/x['path']).read_bytes()
        self.validate=Mock()
    def read_bytes(self,root,path,limit):
        raw=self.objects.values[root+'/'+path]
        if len(raw)>limit:raise ValueError('oversized')
        return raw
    def inspect(self):
        return subject.inspect(self.store,self.plan['operation'],self.canonical,self.shard,
            protections=self.protections,objects=self.objects,revalidate=self.validate,read_bytes=self.read_bytes,
            checkpointed_at='2026-11-09T01:00:00Z')
    def test_selected_row_only_and_old_manifest_remains_recorded(self):
        before=copy.deepcopy(self.canonical);result=self.inspect()
        self.assertEqual(self.canonical,before)
        self.assertEqual(result['changed_keys'],[self.old['key']])
        self.assertEqual(result['previous_manifests'][self.old['key']],self.old['manifest'])
        entry=next(row for row in result['candidate']['files'] if row['key']==self.old['key'])
        self.assertNotEqual(entry['manifest'],self.old['manifest'])
        self.assertEqual(entry['chapter_count'],self.report['sources'][0]['chapter_count'])
        self.assertFalse(result['index_written']);self.assertFalse(result['pointer_written'])
        self.assertFalse(result['publication_ready']);self.assertEqual(self.validate.call_count,2)
    def test_stale_index_protection_or_object_blocks_without_writes(self):
        self.canonical['files'][1]['manifest']='changed'
        with self.assertRaisesRegex(ValueError,'index parent differs'):self.inspect()
        self.canonical['files'][1]['manifest']='preserve'
        action=self.preview['actions'][0];self.protections.values[action['protection_path']]=b'foreign'
        with self.assertRaisesRegex(ValueError,'protection is missing or altered'):self.inspect()
        self.assertFalse(any(event[0].startswith('create-') for event in self.events))
    def test_manifest_bytes_must_be_exact_and_parseable(self):
        self.read_bytes=lambda *_:b'bad'
        with self.assertRaisesRegex(ValueError,'manifest bytes differ'):self.inspect()
    def test_stale_or_malformed_checkpoint_times_are_rejected(self):
        for value, old in (('2026-11-09T01:00:00Z','2026-11-09T01:00:00+00:00'),
                           ('2026-11-09T01:00:00Z','2026-11-09T02:00:00+00:00'),
                           ('2026-11-09T01:00:00','2026-11-08T00:00:00+00:00'),
                           ('2026-11-09T01:00:00Z','2026-11-08T00:00:00')):
            with self.subTest(value=value,old=old),self.assertRaises(ValueError):
                subject._strictly_newer(value,old)
        self.assertEqual(subject._strictly_newer('2026-11-09T01:00:00Z','2026-11-08T00:00:00+00:00'),
                         '2026-11-09T01:00:00Z')

    def test_shard_and_canonical_parent_mismatch_is_rejected(self):
        # Update both expected hashes in a fresh plan is covered by plan
        # validation; here we verify a changed shard never passes its parent.
        self.shard['files'][0]['source_revision']='f'*40
        with self.assertRaisesRegex(ValueError,'index parent differs'):self.inspect()


if __name__=='__main__':unittest.main()
