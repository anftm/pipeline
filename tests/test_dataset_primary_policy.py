"""Pinned dataset originals are references, never uploads to Reader buckets."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
import os
import yaml
from pathlib import Path
from unittest.mock import Mock, patch

import pymupdf

from scripts import pdf_reading_v3 as v3, publish_reader_v3 as publish
from scripts import reader_gc_graph as gc, reader_v3_automation as automation, reader_v3_correction as correction
from tests.test_reader_gc_graph import MemoryStore
from tests.test_publish_reader_v3 import Store


def document():
    with pymupdf.open() as doc:
        doc.new_page(width=100, height=200)
        return doc.tobytes(no_new_id=True)


def dataset_ref(raw, extension='pdf'):
    return {'kind': 'dataset-source', 'repo': 'VoiceOfML/books',
            'path': f'folder/book.{extension}', 'revision': 'a' * 40,
            'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'role': 'runtime'}


@patch.dict(os.environ, {'READER_DATASET_PRIMARY_ENABLED': 'true'})
class DatasetPrimaryPolicyTests(unittest.TestCase):
    def test_dataset_rollout_flag_is_opt_in_on_both_publisher_and_builder(self):
        workflows=Path(__file__).resolve().parents[1]/'.github/workflows'
        for name in ('reader-v3-publish.yml','reader-v3-build.yml'):
            workflow=yaml.safe_load((workflows/name).read_text())
            steps=[step for job in workflow['jobs'].values() for step in job.get('steps',[])]
            values=[step.get('env',{}).get('READER_DATASET_PRIMARY_ENABLED') for step in steps
                    if 'READER_DATASET_PRIMARY_ENABLED' in step.get('env',{})]
            self.assertEqual(values,["${{ vars.READER_DATASET_PRIMARY_ENABLED || 'false' }}"])

    def test_automation_never_creates_a_bucket_primary_for_original_pdf_or_djvu(self):
        for ext in ('pdf', 'djvu'):
            with self.subTest(ext=ext):
                ref = dataset_ref(b'original bytes', ext)
                key = ref['repo'] + '\0' + ref['path']
                entry = {'key': key, 'repo': ref['repo'], 'path': ref['path'],
                    'source_kind': 'upstream', 'source_revision': ref['revision'],
                    'source_format': ext, 'source_sha256': ref['sha256'],
                    'source_bytes': ref['bytes'], 'page_count': 1, 'status': 'ready'}
                spec, count = automation.primary_spec(key, entry)
                self.assertEqual(count, 1)
                self.assertEqual(spec['primary'], ref)
                self.assertNotIn('primary_source', spec)

    def test_upstream_document_policy_never_generates_reader_images(self):
        from scripts import djvu_pages, pdf_assets, pdf_ocr, pdf_ocr_stages as stages
        self.assertTrue(hasattr(djvu_pages, 'render_page'))
        for suffix in ('pdf', 'djvu'):
            with tempfile.TemporaryDirectory() as directory:
                work=Path(directory);src=work/('book.'+suffix)
                raw=document() if suffix=='pdf' else b'AT&TFORM-placeholder'
                src.write_bytes(raw)
                item={'key':'VoiceOfML/books\0book.'+suffix, 'source_format':suffix,
                      'source_kind':'upstream','source_revision':'a'*40,
                      'probe':{'page_count':1,'page_chars':[0],'classification':'scan'}}
                def no_ocr_image(*args,**kwargs):
                    from PIL import Image
                    target=Path(args[2])/'page-000001.png';Image.new('L',(5,5),255).save(target)
                    return target,5,5
                with patch.object(djvu_pages,'sniff',return_value='djvu'), \
                     patch.object(djvu_pages,'inspect',return_value=[{'width':5,'height':5,'dpi':300,'rotation':0}]), \
                     patch.object(djvu_pages,'reader_presentation',return_value={'strategy':'preserve-djvu'}), \
                     patch.object(djvu_pages,'render_page',side_effect=no_ocr_image), \
                     patch.object(pdf_ocr,'render_page',side_effect=no_ocr_image), \
                     patch.object(pdf_ocr,'encode_jxl',side_effect=AssertionError('reader page output forbidden')):
                    result=stages.render_book(item,src,work/'bundle')
                self.assertIsNone(result['page_manifest'])
                self.assertFalse(result['image_rendered'])
                files=[p for p in (work/'bundle').rglob('*') if p.is_file()]
                self.assertEqual(sum('/ocr-input/' in p.as_posix() for p in files),1)
                self.assertFalse(any('/pages/' in p.as_posix() or p.suffix=='.jxl' for p in files))

    def test_external_primary_validation_rejects_unpinned_and_unsafe_sources(self):
        ref = dataset_ref(document())
        key = ref['repo'] + '\0' + ref['path']
        v3.validate_dataset_primary(ref, key, ref['sha256'])
        variants = [
            {**ref, 'revision': 'main'}, {**ref, 'repo': 'other/books'}, {**ref, 'repo':'VoiceOfML/..'},
            {**ref, 'path': '../book.pdf'}, {**ref, 'path': 'folder/%2e%2e/book.pdf'},
            {**ref, 'path': '/absolute.pdf'}, {**ref, 'path': 'folder/book.pdf?x'},
            {**ref, 'path': 'folder/book.html'}, {**ref, 'role': 'provenance'},
            {**ref, 'bytes': True}, {**ref, 'bytes': 512 * 1024 * 1024 + 1},
            {**ref, 'bucket': 'vomebook/pdf-pages-v2'}, {**ref, 'path': 'folder/\ud800.pdf'},
        ]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                v3.validate_dataset_primary(variant, key, ref['sha256'])
        with self.assertRaises(ValueError):
            v3.validate_dataset_primary(ref, 'VoiceOfML/other\0folder/book.pdf', ref['sha256'])
        with self.assertRaises(ValueError):
            v3.validate_dataset_primary(ref, key, 'b' * 64)
        with self.assertRaises(ValueError):
            v3.resource(ref)  # no external source allowed where a bucket object is required
        upper = {**ref, 'path': 'folder/book.PDF'}
        v3.validate_dataset_primary(upper, upper['repo']+'\0'+upper['path'], upper['sha256'])
        self.assertEqual(v3.primary_kind(upper), 'pdf')

    def test_pdf_build_verify_and_stage_never_uploads_primary(self):
        raw = document()
        ref = dataset_ref(raw)
        spec = {'source_key': ref['repo'] + '\0' + ref['path'], 'source_sha256': ref['sha256'],
                'primary': ref}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cached = root / 'source.pdf'; cached.write_bytes(raw)
            bundle = root / 'bundle'; bundle.mkdir()
            def offline_read(requested):
                if requested.get('kind') == 'dataset-source':
                    self.assertEqual(requested, ref)
                    return raw
                return (bundle / requested['path']).read_bytes()
            reading, manifest = v3.build_reading(spec, offline_read, bundle)
            self.assertEqual(manifest['primary']['resource'], ref)
            self.assertEqual(v3.verify_reading(reading, offline_read), manifest)
            self.assertFalse(list(bundle.rglob('*.pdf')))
            store = Store()
            with patch.object(publish, 'read_dataset_primary', return_value=raw):
                report = publish.stage(store, reading, bundle, apply=True)
            self.assertTrue(report['applied'])
            self.assertFalse(any(path.endswith(('.pdf', '.djvu')) for _, path in store.writes))
            candidate = publish.read_index(store, report['candidate'])
            self.assertEqual(candidate['dataset_sources'], [ref])
            self.assertTrue(all('bucket' in item for item in candidate['resources']))

    def test_changed_dataset_bytes_stop_publication_before_any_write(self):
        raw = document()
        ref = dataset_ref(raw)
        spec = {'source_key': ref['repo'] + '\0' + ref['path'], 'source_sha256': ref['sha256'], 'primary': ref}
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            def offline_read(requested):
                return raw if requested.get('kind') == 'dataset-source' else (bundle / requested['path']).read_bytes()
            reading, _ = v3.build_reading(spec, offline_read, bundle)
            store = Store()
            with patch.object(publish, 'read_dataset_primary', return_value=b'changed'):
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    publish.stage(store, reading, bundle, apply=True)
            self.assertEqual(store.writes, [])

    def test_dataset_rollout_is_off_by_default_until_both_readers_are_ready(self):
        raw=document();ref=dataset_ref(raw);store=Store()
        spec={'source_key':ref['repo']+'\0'+ref['path'],'source_sha256':ref['sha256'],'primary':ref}
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory)
            read=lambda item:raw if item.get('kind')=='dataset-source' else (bundle/item['path']).read_bytes()
            reading,_=v3.build_reading(spec,read,bundle)
            with patch.object(publish,'read_dataset_primary',return_value=raw):
                with patch.dict(os.environ,{'READER_DATASET_PRIMARY_ENABLED':'false'}):
                    dry=publish.stage(store,reading,bundle,apply=False)
                    self.assertFalse(dry['applied'])
                    with self.assertRaisesRegex(ValueError,'dataset primary rollout'):
                        publish.build_stage(store,{**spec,'preview_only':True},bundle/'build',apply=True)
                    with self.assertRaisesRegex(ValueError,'dataset primary rollout'):
                        publish.stage(store,reading,bundle,apply=True)
                    self.assertEqual(store.writes,[])
                staged=publish.stage(store,reading,bundle,apply=True)
                before=list(store.writes)
                with patch.dict(os.environ,{'READER_DATASET_PRIMARY_ENABLED':'false'}):
                    with self.assertRaisesRegex(ValueError,'dataset primary rollout'):
                        publish.promote(store,staged['candidate'],None,apply=True)
                self.assertEqual(store.writes,before)

    def test_rollback_checks_pinned_original_and_fails_without_mutation_if_changed(self):
        raw=document();ref=dataset_ref(raw);store=Store()
        spec={'source_key':ref['repo']+'\0'+ref['path'],'source_sha256':ref['sha256'],'primary':ref}
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory);read=lambda item:raw if item.get('kind')=='dataset-source' else (bundle/item['path']).read_bytes()
            reading,_=v3.build_reading(spec,read,bundle)
            with patch.object(publish,'read_dataset_primary',return_value=raw):
                staged=publish.stage(store,reading,bundle,apply=True)
                one=publish.promote(store,staged['candidate'],None,apply=True)
                second=publish.commit_catalog(store,{'version':3,'kind':'reader-reading-catalog',
                    'files':{},'parent_generation':one['generation']},one['pointer'],apply=True)
                before=list(store.writes)
                with patch.object(publish,'read_dataset_primary',return_value=b'changed'):
                    with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                        publish.rollback(store,one['pointer']['catalog'],second['generation'],apply=True)
                self.assertEqual(store.writes,before)
                with patch.dict(os.environ,{'READER_DATASET_PRIMARY_ENABLED':'false'}):
                    with self.assertRaisesRegex(ValueError,'dataset primary rollout'):
                        publish.rollback(store,one['pointer']['catalog'],second['generation'],apply=True)
                self.assertEqual(store.writes,before)
                result=publish.rollback(store,one['pointer']['catalog'],second['generation'],apply=True)
                self.assertTrue(result['applied'])
                self.assertFalse(any(path.endswith(('.pdf','.djvu')) for _,path in store.writes))

    def test_legacy_copy_job_fails_before_download_or_write(self):
        raw = document(); source = hashlib.sha256(raw).hexdigest()
        root = f'objects/{source[:2]}/{source}/' + 'b' * 16
        spec = {'source_key': 'VoiceOfML/books\0folder/book.pdf', 'source_sha256': source,
                'primary': publish.metadata(v3.shared.PDF_PAGES_BUCKET, root+'/document.pdf', raw),
                'primary_source': {'repo': 'VoiceOfML/books', 'path': 'folder/book.pdf', 'revision': 'a'*40},
                'preview_only': True}
        with tempfile.TemporaryDirectory() as directory, \
             patch('huggingface_hub.HfApi', side_effect=AssertionError('must not contact source')), \
             patch('huggingface_hub.hf_hub_download', side_effect=AssertionError('must not download')):
            store = Store()
            with self.assertRaisesRegex(ValueError, 'dataset primary'):
                publish.build_stage(store, spec, Path(directory), apply=True)
            self.assertEqual(store.writes, [])

    def test_old_staged_bucket_copy_cannot_be_promoted_as_new_original(self):
        raw=document();source=hashlib.sha256(raw).hexdigest();key='VoiceOfML/books\0folder/book.pdf';store=Store()
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory);root=Path(f'objects/{source[:2]}/{source}/'+'b'*16)
            target=bundle/root/'document.pdf';target.parent.mkdir(parents=True);target.write_bytes(raw)
            primary=publish.metadata(gc.PDF_PAGES_BUCKET,target.relative_to(bundle).as_posix(),raw)
            reading,_=v3.build_reading({'source_key':key,'source_sha256':source,'primary':primary},
                lambda r:(bundle/r['path']).read_bytes(),bundle)
            for path in bundle.rglob('*'):
                if path.is_file():store.objects[(gc.PDF_PAGES_BUCKET,path.relative_to(bundle).as_posix())]=path.read_bytes()
            raw_manifest=store.read_bytes(reading['bucket'],reading['path']);manifest=v3.decode(raw_manifest)
            body={'version':1,'kind':'reader-reading-candidate','source_key':key,'source_sha256':source,
                  'reading_generation':manifest['generation'],'reading':reading,'components':manifest['components'],
                  'page_count':manifest['page_count'],'resources':[primary,reading,manifest['page_map']]}
            candidate_raw=publish.encode(body)
            candidate=publish.metadata(publish.ASSETS,'reader-index/v3/candidates/'+v3.text.digest(body)+'.json',candidate_raw)
            store.objects[(candidate['bucket'],candidate['path'])]=candidate_raw
            with self.assertRaisesRegex(ValueError,'dataset primary'):
                publish.promote(store,candidate,None,apply=True)
            self.assertEqual(store.writes,[])

    def test_caller_supplied_bucket_original_cannot_bypass_copy_barrier(self):
        raw = document(); source = hashlib.sha256(raw).hexdigest()
        root = f'objects/{source[:2]}/{source}/' + 'b' * 16
        spec = {'source_key': 'VoiceOfML/books\0folder/book.pdf', 'source_sha256': source,
                'primary': publish.metadata(v3.shared.PDF_PAGES_BUCKET, root+'/document.pdf', raw)}
        with tempfile.TemporaryDirectory() as directory:
            store = Store()
            with self.assertRaisesRegex(ValueError, 'dataset primary'):
                publish.build_stage(store, spec, Path(directory), apply=True)
            self.assertEqual(store.writes, [])

    def test_direct_stage_cannot_upload_locally_supplied_original_copy(self):
        raw=document();source=hashlib.sha256(raw).hexdigest();store=Store()
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory);root=Path(f'objects/{source[:2]}/{source}/'+'b'*16)
            target=bundle/root/'document.pdf';target.parent.mkdir(parents=True);target.write_bytes(raw)
            primary=publish.metadata(gc.PDF_PAGES_BUCKET,target.relative_to(bundle).as_posix(),raw)
            spec={'source_key':'VoiceOfML/books\0folder/book.pdf','source_sha256':source,'primary':primary}
            reading,_=v3.build_reading(spec,lambda ref:(bundle/ref['path']).read_bytes(),bundle)
            with self.assertRaisesRegex(ValueError,'dataset primary'):
                publish.stage(store,reading,bundle,apply=True)
            self.assertEqual(store.writes,[])
            # Legacy routing remains valid when the original is already remote;
            # only new local copies are forbidden.
            store.objects[(primary['bucket'],primary['path'])]=raw
            target.unlink()
            result=publish.stage(store,reading,bundle,apply=True)
            self.assertTrue(result['applied'])
            self.assertFalse(any(path.endswith('.pdf') for _,path in store.writes))

    def test_gc_blocks_unverified_external_primary_instead_of_shrinking_graph(self):
        store = MemoryStore()
        store.put(gc.READER_ASSETS_BUCKET, 'reader-index/source-example.json', {
            'version': 1, 'kind': 'reader-v3-automation', 'tasks': {
                'task': {'spec': {'primary': {**dataset_ref(b'data'), 'revision': 'main'}}}}, 'days': {}})
        report = gc.ReferenceGraph(store).build()
        self.assertFalse(report['graph_complete'])
        self.assertTrue(any('invalid dataset primary' in blocker for blocker in report['blockers']))
        self.assertTrue(all(not bucket['candidates'] for bucket in report['buckets'].values()))

    def test_streamed_gc_manifest_also_fails_closed_on_dataset_primary(self):
        from scripts.reader_gc_stream import graph_json
        raw=document();ref=dataset_ref(raw);source=ref['sha256'];key=ref['repo']+'\0'+ref['path']
        class StreamingStore(MemoryStore):
            def read_graph_json(self,bucket,path,parser=graph_json):
                value=self.read_bytes(bucket,path)
                return parser([value],path.endswith('.gz'))
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory);read=lambda item:raw if item.get('kind')=='dataset-source' else (bundle/item['path']).read_bytes()
            reading,_=v3.build_reading({'source_key':key,'source_sha256':source,'primary':ref},read,bundle)
            store=StreamingStore()
            for path in bundle.rglob('*'):
                if path.is_file():store.put(gc.PDF_PAGES_BUCKET,path.relative_to(bundle).as_posix(),path.read_bytes())
            store.put(gc.READER_ASSETS_BUCKET,'reader-index/reader_assets.json.gz',{'v':1,'f':{key:{
                's':2,'m':'p','p':reading['path'],'b':gc.PDF_PAGES_BUCKET}}})
            report=gc.ReferenceGraph(store).build()
            self.assertFalse(report['graph_complete'])
            self.assertTrue(any('dataset primary availability not verified' in b for b in report['blockers']))
            self.assertTrue(all(not bucket['candidates'] for bucket in report['buckets'].values()))

    def test_current_collector_rejects_dataset_primary_from_pinned_candidate_closure(self):
        from scripts import reader_gc_collect as collect
        raw=document();ref=dataset_ref(raw);source=ref['sha256'];key=ref['repo']+'\0'+ref['path'];store=Store()
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory)
            read=lambda item:raw if item.get('kind')=='dataset-source' else (bundle/item['path']).read_bytes()
            reading,_=v3.build_reading({'source_key':key,'source_sha256':source,'primary':ref},read,bundle)
            with patch.object(publish,'read_dataset_primary',return_value=raw):
                staged=publish.stage(store,reading,bundle,apply=True)
                publish.promote(store,staged['candidate'],None,apply=True)
            graph_store=MemoryStore()
            for (bucket,path),value in store.objects.items():graph_store.objects[bucket][path]=value
            graph=gc.ReferenceGraph(graph_store);report=graph.build()
            self.assertFalse(report['graph_complete'])
            self.assertTrue(any('dataset primary availability not verified' in b for b in report['blockers']))
            graph_store.delete=Mock(side_effect=AssertionError('deletion forbidden'))
            with self.assertRaisesRegex(RuntimeError,'graph is incomplete'):
                collect.collect(graph_store,Mock(),graph,report,{},apply=True,reader_only=True)
            graph_store.delete.assert_not_called()

    def test_old_published_bucket_primary_can_be_reused_without_route_change(self):
        raw = document(); ref = dataset_ref(raw)
        source = ref['sha256']; path = f'objects/{source[:2]}/{source}/'+'b'*16+'/document.pdf'
        old = publish.metadata(v3.shared.PDF_PAGES_BUCKET, path, raw)
        self.assertTrue(publish.same_primary_identity(old, ref))
        self.assertFalse(publish.same_primary_identity({**old, 'sha256':'b'*64}, ref))
        self.assertFalse(publish.same_primary_identity({**old, 'bytes':old['bytes']+1}, ref))

    def test_old_bucket_primary_is_preserved_in_full_additive_ocr_upgrade(self):
        raw=document();ref=dataset_ref(raw);source=ref['sha256'];key=ref['repo']+'\0'+ref['path'];store=Store()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old_root=Path(f'objects/{source[:2]}/{source}/'+'b'*16)
            primary=publish.metadata(gc.PDF_PAGES_BUCKET,(old_root/'document.pdf').as_posix(),raw)
            store.objects[(primary['bucket'],primary['path'])]=raw
            old_bundle=root/'old';old_bundle.mkdir()
            old_read=lambda requested: store.read_bytes(requested['bucket'],requested['path'])
            old_ref,_=v3.build_reading({'source_key':key,'source_sha256':source,'primary':primary},old_read,old_bundle)
            staged=publish.stage(store,old_ref,old_bundle,apply=True)
            old_manifest=v3.decode((old_bundle/old_ref['path']).read_bytes())
            # Historical route was published before the new no-copy policy.
            first=publish.commit_catalog(store,{'version':3,'kind':'reader-reading-catalog',
                'files':{key:{'resource':old_ref,'source_sha256':source,'reading_generation':old_manifest['generation'],
                              'page_count':old_manifest['page_count'],'components':old_manifest['components']}},
                'parent_generation':None},None,apply=True)
            from tests.test_pdf_text_layer import raw_page
            layer=v3.text.from_page(raw_page('source'),source)
            text_bundle=root/'text';text_bundle.mkdir()
            text_ref=v3.build_text_bundle([layer],[{'p':1}],Path(f'objects/{source[:2]}/{source}/'+'c'*16),text_bundle)
            for path in text_bundle.rglob('*'):
                if path.is_file():store.objects[(gc.PDF_PAGES_BUCKET,path.relative_to(text_bundle).as_posix())]=path.read_bytes()
            ocr_raw=publish.encode({'source_sha256':source,'page_count':1,'text_layer':text_ref})
            ocr_ref=publish.metadata(gc.PDF_PAGES_BUCKET,f'objects/{source[:2]}/{source}/'+'d'*16+'/ocr-manifest.json',ocr_raw,'provenance')
            store.objects[(ocr_ref['bucket'],ocr_ref['path'])]=ocr_raw
            writes=len(store.writes)
            result=publish.build_stage(store,{'source_key':key,'source_sha256':source,'primary':ref,
                'ocr_manifest':ocr_ref,'reuse_reading':old_ref},root/'new',apply=True)
            publish.promote(store,result['candidate'],first['generation'],apply=True)
            new=publish.current(store)[1]['files'][key]['resource']
            reader=publish.CandidateReader(store)
            try:manifest=v3.verify_reading(new,reader)
            finally:reader.close()
            self.assertEqual(manifest['primary']['resource'],primary)
            self.assertEqual(manifest['text_layer'],text_ref)
            self.assertFalse(any(path.endswith(('.pdf','.djvu')) for _,path in store.writes[writes:]))

    def test_offline_verifier_reads_explicit_local_dataset_fixture_only(self):
        raw=document();ref=dataset_ref(raw)
        with tempfile.TemporaryDirectory() as directory:
            mirror=Path(directory);target=mirror/'dataset-sources'/ref['sha256']
            target.parent.mkdir();target.write_bytes(raw)
            self.assertEqual(v3.verified_primary(ref,v3.local_reader(mirror),ref['repo']+'\0'+ref['path'],ref['sha256']),raw)
            target.write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                v3.verified_primary(ref,v3.local_reader(mirror),ref['repo']+'\0'+ref['path'],ref['sha256'])

    def test_dataset_reader_detects_changed_identity_before_cache_hit(self):
        raw = document(); ref = dataset_ref(raw)
        reader = publish.CandidateReader(Store())
        self.addCleanup(reader.close)
        with patch.object(publish, 'read_dataset_primary', return_value=raw) as fetch:
            self.assertEqual(reader(ref), raw)
            self.assertEqual(reader(ref), raw)
            fetch.assert_called_once_with(ref)
            with self.assertRaisesRegex(ValueError, 'conflicting pinned dataset source'):
                reader({**ref, 'sha256':'b'*64})

    def test_pdf_correction_renders_from_exact_external_primary(self):
        raw = document(); ref = dataset_ref(raw); source = ref['sha256']
        from tests.test_pdf_text_layer import raw_page
        layer = v3.text.from_page(raw_page('source'), source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bundle = root/'bundle'; bundle.mkdir();store = Store()
            layer_ref = v3.build_text_bundle([layer], [{'p':1}],
                Path(f'objects/{source[:2]}/{source}/'+'e'*16), bundle)
            spec = {'source_key':ref['repo']+'\0'+ref['path'], 'source_sha256':source,
                    'primary':ref, 'text_layer':layer_ref}
            read = lambda item: raw if item.get('kind')=='dataset-source' else (bundle/item['path']).read_bytes()
            reading, _ = v3.build_reading(spec, read, bundle)
            for path in bundle.rglob('*'):
                if path.is_file():store.objects[(gc.PDF_PAGES_BUCKET,path.relative_to(bundle).as_posix())]=path.read_bytes()
            manifest = v3.decode(store.read_bytes(reading['bucket'], reading['path']))
            index = v3.decode(store.read_bytes(layer_ref['bucket'], layer_ref['path']))
            part = v3.decode(store.read_bytes(index['partitions'][0]['resource']['bucket'], index['partitions'][0]['resource']['path']))
            task = {'text_layer':part['pages'][0]['resource'], 'reading':reading,
                    'base_generation':layer['generation'], 'page':1, 'issues':[]}
            work=root/'review';work.mkdir()
            with patch.object(publish, 'read_dataset_primary', return_value=raw):
                _, _, evidence = correction.prepare(store,task,work)
            self.assertTrue((work/'page.png').is_file())
            self.assertEqual(evidence['primary'],manifest['primary']['resource'])
            self.assertEqual(store.writes,[])

    @unittest.skipUnless(all(shutil.which(name) for name in ('cjb2','c44','djvm','djvused','ddjvu','djvutxt')),
                         'DjVuLibre tools unavailable')
    def test_djvu_correction_renders_only_temporary_evidence_not_bucket_original(self):
        from tests.test_djvu_pages import build_djvu
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);raw=build_djvu(root).read_bytes();ref=dataset_ref(raw,'djvu')
            source=ref['sha256'];mapping=v3.djvu_pages.page_map(raw,source);geometry=mapping['pages'][0]
            layer=v3.text.from_page({'version':1,'kind':'pdf-ocr-page','page':1,
                'width':geometry['width'],'height':geometry['height'],
                'source':'ocr','text':'','blocks':[]},source)
            second=v3.text.from_page({'version':1,'kind':'pdf-ocr-page','page':2,
                'width':mapping['pages'][1]['width'],'height':mapping['pages'][1]['height'],
                'source':'ocr','text':'','blocks':[]},source)
            bundle=root/'bundle';bundle.mkdir();store=Store()
            layer_ref=v3.build_text_bundle([layer,second],[{'p':1},{'p':2}],
                Path(f'objects/{source[:2]}/{source}/'+'e'*16),bundle)
            spec={'source_key':ref['repo']+'\0'+ref['path'],'source_sha256':source,
                  'primary':ref,'text_layer':layer_ref}
            read=lambda item:raw if item.get('kind')=='dataset-source' else (bundle/item['path']).read_bytes()
            reading,_=v3.build_reading(spec,read,bundle)
            for path in bundle.rglob('*'):
                if path.is_file():store.objects[(gc.PDF_PAGES_BUCKET,path.relative_to(bundle).as_posix())]=path.read_bytes()
            index=v3.decode(store.read_bytes(layer_ref['bucket'],layer_ref['path']))
            part=v3.decode(store.read_bytes(index['partitions'][0]['resource']['bucket'],index['partitions'][0]['resource']['path']))
            task={'text_layer':part['pages'][0]['resource'],'reading':reading,
                  'base_generation':layer['generation'],'page':1,'issues':[]}
            work=root/'review';work.mkdir()
            with patch.object(publish,'read_dataset_primary',return_value=raw):
                _,_,evidence=correction.prepare(store,task,work)
            self.assertTrue((work/'page.png').is_file())
            self.assertEqual(evidence['render_policy'],'djvu-source-max-pixels-v1')
            self.assertEqual(store.writes,[])
            self.assertFalse(any(path.endswith('.djvu') for _,path in store.objects))

    def test_gc_ignores_external_source_as_bucket_path_and_keeps_bucket_artifacts(self):
        raw = document(); ref = dataset_ref(raw)
        spec = {'source_key': ref['repo'] + '\0' + ref['path'], 'source_sha256': ref['sha256'], 'primary': ref}
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            def offline_read(requested):
                return raw if requested.get('kind') == 'dataset-source' else (bundle / requested['path']).read_bytes()
            reading, manifest = v3.build_reading(spec, offline_read, bundle)
            store = MemoryStore()
            for path in bundle.rglob('*'):
                if path.is_file():
                    store.put(gc.PDF_PAGES_BUCKET, path.relative_to(bundle).as_posix(), path.read_bytes())
            store.put(gc.READER_ASSETS_BUCKET, 'reader-index/reader_assets.json.gz', {'v': 1, 'f': {
                spec['source_key']: {'s': 2, 'm': 'p', 'p': reading['path'], 'b': gc.PDF_PAGES_BUCKET}}})
            graph = gc.ReferenceGraph(store); report = graph.build()
            self.assertFalse(report['graph_complete'])
            self.assertTrue(any('dataset primary availability not verified' in b for b in report['blockers']))
            self.assertEqual(report['missing'], [])
            self.assertIn((gc.PDF_PAGES_BUCKET, manifest['page_map']['path']), graph.references)
            self.assertTrue(all(not bucket['candidates'] for bucket in report['buckets'].values()))


if __name__ == '__main__':
    unittest.main()
