"""Disjoint local EPUB batches preserve exact files, failure receipts and old routes."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import reader_epub_batches as subject, reader_epub_plan, reader_epub_shadow as shadow, reader_stream_index, search_snapshot
from tests.test_reader_epub_shadow import epub, plan
from tests.test_reader_epub_plan import data, REPO, OLD, NEW, SHA0, SHA1


def sample_plans():
    values=list(copy.deepcopy(data()))
    summary,details,corpus,revisions,canonical,shards,number=values
    names=[]
    for n in range(500):
        name=f'book-{n:03d}'
        if reader_stream_index.shard_for(REPO+'\0'+name+'.epub',8)==number:
            names.append(name)
            if len(names)==7:break
    key=lambda name:REPO+'\0'+name+'.epub'
    corpus['rc']=[[0,name,'epub',0,10,0] for name in names]
    summary['formats'][0].update(sources=len(names),source_content_changed=len(names),definite_backlog=len(names))
    details['rows']=[{'key':key(name),'source_revision':NEW,'previous_revision':OLD,
                      'previous_source_sha256':SHA0,'current_source_sha256':SHA1,
                      'state':'content_changed','proof':'current-lfs-sha256','recorded_failure':False} for name in names]
    canonical['files']=[{'key':key(name),'source_revision':OLD,'source_sha256':SHA0,
                         'manifest':f'chapters/ebook/epub/{name}/chapter-manifest.json'} for name in names]
    return values


class BatchSelectionTests(unittest.TestCase):
    def test_offset_chunks_are_disjoint_stable_and_preserve_old_routes(self):
        summary,details,corpus,revisions,canonical,shards,shard=sample_plans()
        chunks=[reader_epub_plan.create(summary,details,corpus,revisions,canonical,shards,
                shard=shard,operation=f'{i+1:032x}',offset=i*3) for i in range(3)]
        self.assertEqual([len(x['sources']) for x in chunks],[3,3,1])
        self.assertNotIn('selection_offset',chunks[0]);self.assertEqual(chunks[1]['selection_offset'],3)
        keys=[row['key'] for c in chunks for row in c['sources']]
        self.assertEqual(len(set(keys)),7)
        self.assertTrue(all(row['previous_manifest'] for c in chunks for row in c['sources']))
        for chunk in chunks:shadow._verify_plan(chunk)
        with self.assertRaisesRegex(ValueError,'offset has no eligible'):
            reader_epub_plan.create(summary,details,corpus,revisions,canonical,shards,shard=shard,operation='a'*32,offset=9)
        for bad in (-1,True,1.2,100_001):
            with self.subTest(offset=bad),self.assertRaises(ValueError):
                reader_epub_plan.create(summary,details,corpus,revisions,canonical,shards,shard=shard,operation='a'*32,offset=bad)
    def test_snapshot_capture_creates_repeatable_complete_chunks(self):
        summary,details,corpus,revisions,canonical,shards,shard=sample_plans()
        backlog=reader_epub_plan.backlog;pointer=b'exact-pointer';hub=Mock()
        hub.read_bytes.return_value=pointer
        api=Mock();api.repo_info.return_value=Mock(sha=NEW)
        def read(_fs,_bucket,path):
            if path.endswith('/index.json'):return canonical
            number=int(path.rsplit('-',1)[1].split('.')[0]);return shards[number]
        with patch.object(backlog,'inspect',return_value=(summary,details)), \
                patch.object(backlog,'capture_search',return_value=(pointer,None,corpus,revisions,None)), \
                patch.object(backlog.search_publication_pointer,'decode_pointer',return_value={'desired':summary['search_generation']}), \
                patch.object(backlog.matrix,'read_json',side_effect=read), \
                patch.object(backlog.publication,'current',return_value=({'generation':summary['reader_generation']},{})):
            plans=reader_epub_plan.inspect_batches(hub=hub,api=api,fs=Mock(),client=Mock(),shard=shard)
            selections=[(p.get('selection_offset',0),p['operation']) for p in plans]
            again=reader_epub_plan.inspect_batches(hub=hub,api=api,fs=Mock(),client=Mock(),shard=shard,selections=selections)
        self.assertEqual(plans,again);self.assertEqual([len(p['sources']) for p in plans],[3,3,1])
        self.assertEqual(len({p['operation'] for p in plans}),3)

    def test_offset_must_be_bound_in_plan_hash(self):
        source=epub();p=plan(source);p['selection_offset']=3
        with self.assertRaisesRegex(ValueError,'identity differs'):shadow._verify_plan(p)
        p['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in p.items() if k!='plan_sha256'})).hexdigest()
        self.assertIs(shadow._verify_plan(p),p)
        p['selection_offset']=True
        p['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in p.items() if k!='plan_sha256'})).hexdigest()
        with self.assertRaises(ValueError):shadow._verify_plan(p)


class BatchRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.output=self.root/'output';self.export=self.root/'artifact'
        self.raw=epub();first=plan(self.raw);second=copy.deepcopy(first)
        second['operation']='e'*32;second['selection_offset']=3
        second['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in second.items() if k!='plan_sha256'})).hexdigest()
        self.plans=[first,second]
    def run_batch(self,fetch=None):
        return subject.run(hub=Mock(),api=Mock(),fs=Mock(),client=Mock(),shard=self.plans[0]['shard'],
             output=self.output,fetch=fetch or (lambda _u,p,_t:p.write_bytes(self.raw)))
    def test_distinct_batch_results_export_exact_files_not_native_epub(self):
        with patch.object(subject.planning,'inspect_batches',side_effect=[self.plans,self.plans]) as inspect:
            result=self.run_batch()
        self.assertEqual(inspect.call_count,2);self.assertTrue(result['snapshot_revalidated'])
        self.assertEqual(result['converted_sources'],2);self.assertEqual(result['failed_sources'],0)
        self.assertEqual(result['remote_writes'],0);self.assertFalse(result['publication_ready'])
        export=subject.export_converted(self.output,self.export)
        self.assertEqual(export['exported_sources'],2)
        self.assertFalse(any(p.name=='source.epub' for p in self.export.rglob('*')))
        self.assertTrue((self.export/'epub-converted'/'batch-000003'/'0'/'bundle'/'chapter-manifest.json').is_file())
    def test_failed_batch_receipt_does_not_hide_later_success(self):
        calls=0
        def fetch(_u,target,_t):
            nonlocal calls
            calls+=1
            if calls==1:raise ValueError('untrusted detail must not be included')
            target.write_bytes(self.raw)
        with patch.object(subject.planning,'inspect_batches',side_effect=[self.plans,self.plans]):
            result=self.run_batch(fetch)
        self.assertEqual(result['failed_sources'],1);self.assertEqual(result['converted_sources'],1)
        failure=json.loads((self.output/'batch-000000-failure.json').read_bytes())
        self.assertEqual(failure['error_type'],'ValueError');self.assertNotIn('untrusted',str(failure))
        exported=subject.export_converted(self.output,self.export);self.assertEqual(exported['exported_sources'],1)
    def test_changed_snapshot_or_revalidation_failure_cannot_export(self):
        for later in ([self.plans[0]], ValueError('drift')):
            with self.subTest(later=later), tempfile.TemporaryDirectory() as tmp:
                self.output=Path(tmp)/'out';self.export=Path(tmp)/'export'
                with patch.object(subject.planning,'inspect_batches',side_effect=[self.plans,later]):
                    with self.assertRaisesRegex(ValueError,'changed during'):self.run_batch()
                self.assertFalse(json.loads((self.output/'results.json').read_bytes())['snapshot_revalidated'])
                with self.assertRaisesRegex(ValueError,'fresh independent recheck'):subject.export_converted(self.output,self.export)
    def test_one_bad_book_in_a_group_does_not_block_the_next_book(self):
        first=copy.deepcopy(self.plans[0]);second=copy.deepcopy(first['sources'][0]);second['key']=second['key']+'x'
        first['sources'].append(second)
        original_build=subject.shadow.build;calls=0
        def build(p,directory,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise ValueError('one book is bad')
            # Real source/manifest verification remains active for the valid book.
            p=copy.deepcopy(p);p['sources'][0]=copy.deepcopy(self.plans[0]['sources'][0])
            p['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in p.items() if k!='plan_sha256'})).hexdigest()
            return original_build(p,directory,**kwargs)
        # Exercise per-source scheduling separately; a second valid plan must be
        # attempted even when the first source's conversion fails.
        first['sources']=[copy.deepcopy(first['sources'][0]),copy.deepcopy(first['sources'][0])]
        first['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in first.items() if k!='plan_sha256'})).hexdigest()
        with patch.object(subject.planning,'inspect_batches',side_effect=[[first],[first]]), \
                patch.object(subject.shadow,'build',side_effect=build):
            result=self.run_batch()
        self.assertEqual(calls,2);self.assertEqual(result['failed_sources'],1)
        self.assertEqual(result['converted_sources'],1)

    def test_workflow_is_readonly_retains_converted_files_and_not_sources(self):
        import yaml
        path=Path(__file__).resolve().parents[1]/'.github/workflows/reader-epub-batches.yml'
        flow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']),{'workflow_dispatch'});self.assertEqual(flow['permissions'],{'contents':'read'})
        steps=flow['jobs']['convert']['steps'];commands='\n'.join(x.get('run','') for x in steps)
        self.assertIn('reader_epub_batches.py',commands)
        self.assertNotIn('--apply',commands);self.assertNotIn('git push',commands)
        artifact=next(x for x in steps if 'converted-shard' in x.get('with',{}).get('name',''))
        self.assertEqual(artifact['with']['path'],'output/export/epub-converted/')
        self.assertEqual(artifact['with']['include-hidden-files'],'true')

    def test_recheck_after_timeout_preserves_original_results_and_exports(self):
        with patch.object(subject.planning,'inspect_batches',side_effect=[self.plans,TimeoutError('unavailable')]):
            with self.assertRaises(ValueError):self.run_batch()
        original=(self.output/'results.json').read_bytes();receipt=self.root/'recheck.json'
        with patch.object(subject.planning,'inspect_batches',return_value=self.plans):
            accepted=subject.recheck(self.output,hub=Mock(),api=Mock(),fs=Mock(),client=Mock(),destination=receipt)
        self.assertTrue(accepted['snapshot_revalidated']);self.assertEqual((self.output/'results.json').read_bytes(),original)
        exported=subject.export_converted(self.output,self.export,recheck_receipt=receipt)
        self.assertEqual(exported['exported_sources'],2)
        self.assertTrue((self.export/'epub-converted'/'recheck.json').is_file())

    def test_tampered_local_file_cannot_export(self):
        with patch.object(subject.planning,'inspect_batches',side_effect=[self.plans,self.plans]):self.run_batch()
        file=self.output/'batch-000000'/'0'/'bundle'/'chapter-manifest.json';file.write_bytes(file.read_bytes()+b' ')
        with self.assertRaises(ValueError):subject.export_converted(self.output,self.export)


if __name__=='__main__':unittest.main()
