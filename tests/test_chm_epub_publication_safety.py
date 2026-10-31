import copy
import gzip
import json
import unittest

from scripts import chm_epub_publication_safety as safety, publish_reader_v3 as publication


class ChmPublicationSafetyTests(unittest.TestCase):
    def setUp(self):
        catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                   'parent_generation': 'a' * 64, 'document_reset': 'document-rebuild-20261025'}
        catalog['generation'] = publication.text.digest(catalog)
        raw = publication.encode(catalog)
        ref = publication.metadata(publication.ASSETS,
              f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw)
        pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': catalog['generation'],
                   'catalog': ref, 'resources': [ref]}
        receipt = {'kind': 'reader-document-reset', 'status': 'withdrawn', 'generation': catalog['generation'],
                   'reset_id': catalog['document_reset'], 'base_snapshot': None}
        self.plan = {'new_v3_generation': catalog['generation'],
                     'overrides': {'reader-index/v3/current.json': publication.encode(pointer),
                                   'reader-index/gc/document-reset/withdrawal.json': publication.encode(receipt)},
                     'immutable': {ref['path']: raw}}
        self.reports = {surface: {'version': 1, 'surface': surface, 'active': True,
                        'generation': catalog['generation'], 'catalog_sha256': ref['sha256'],
                        'projection_verified': True} for surface in ('hf', 'pages')}

    def test_both_reader_surfaces_must_accept_exact_catalog(self):
        self.assertEqual(safety.require_projection_acceptance(self.plan, self.reports), {'hf': True, 'pages': True})
        bad = copy.deepcopy(self.reports)
        bad['pages']['catalog_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'has not accepted'):
            safety.require_projection_acceptance(self.plan, bad)

    def test_projection_acceptance_compares_full_routes_on_both_sites(self):
        from unittest.mock import patch
        base = {'v': 1, 'f': {'repo\0book.chm': {'s': 2, 'm': 'e', 'p': 'chapter'}}}
        raw_base = gzip.compress(publication.encode(base), mtime=0)
        base_ref = publication.metadata(publication.ASSETS,
                    f'reader-index/snapshots/{safety.chm.digest(raw_base)}/reader_assets.json.gz', raw_base)
        catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                   'base_sidecar': base_ref, 'parent_generation': 'a' * 64}
        catalog['generation'] = publication.text.digest(catalog)
        raw_catalog = publication.encode(catalog)
        ref = publication.metadata(publication.ASSETS,
                    f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw_catalog)
        pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': catalog['generation'],
                   'catalog': ref, 'resources': [ref]}
        plan = {'new_v3_generation': catalog['generation'],
                'overrides': {'reader-index/v3/current.json': publication.encode(pointer)},
                'immutable': {ref['path']: raw_catalog, base_ref['path']: raw_base}}
        projection = publication.project_sidecar(base, catalog)
        hf = publication.encode(projection)
        pages = gzip.compress(hf, mtime=0)
        receipts = {site: {'version': 1, 'surface': site, 'active': True,
                    'generation': catalog['generation'], 'catalog_sha256': ref['sha256']} for site in ('hf', 'pages')}
        receipts['pages'].update(reader_contract='pdf-reading-v3-v1', sidecar_sha256=safety.chm.digest(pages))
        class Response:
            def __init__(self, content):self.content = content
            def raise_for_status(self):pass
            def json(self):return json.loads(self.content)
        class Client:
            def get(self, url, **kwargs):
                if url.endswith('reader-v3-status'):return Response(publication.encode(receipts['hf']))
                if url.endswith('reader_v3_receipt.json'):return Response(publication.encode(receipts['pages']))
                return Response(pages if url.endswith('.gz') else hf)
        with patch.object(publication, 'current', return_value=(pointer, catalog)):
            accepted = safety.observe_planned_projection(None, Client(), plan)
            self.assertTrue(accepted['pages']['projection_verified'])
            receipts['hf']['generation'] = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'has not accepted'):
                safety.observe_planned_projection(None, Client(), plan)

    def test_document_reset_identity_is_checked_but_does_not_authorize_gc(self):
        result = safety.validate_document_reset_identity(self.plan, self.reports)
        self.assertTrue(result['identity_compatible'])
        self.assertFalse(result['document_collection_authorized'])
        damaged = copy.deepcopy(self.plan)
        receipt = json.loads(damaged['overrides']['reader-index/gc/document-reset/withdrawal.json'])
        receipt['reset_id'] = 'different-reset'
        damaged['overrides']['reader-index/gc/document-reset/withdrawal.json'] = publication.encode(receipt)
        with self.assertRaisesRegex(ValueError, 'identity disagree'):
            safety.validate_document_reset_identity(damaged, self.reports)

    def test_document_reset_prerequisite_rejects_djvu_republication(self):
        from unittest.mock import patch
        from scripts import reader_document_reset as reset
        sidecar = {'v': 1, 'f': {}}
        plan = copy.deepcopy(self.plan)
        plan['overrides'][reset.SIDECAR] = gzip.compress(publication.encode(sidecar), mtime=0)
        class Store:
            def assert_buckets_available(self):pass
            def read_bytes(self, bucket, path):
                if path == 'control/ocr-current.json':raise FileNotFoundError(path)
                if path.endswith('/withdrawal.json'):return publication.encode({'status': 'withdrawn'})
                return publication.encode({'files': []})
        with patch.object(reset, 'REGISTRIES', []), patch.object(reset, 'is_djvu', return_value=False):
            result = safety.verify_document_reset_prerequisites(Store(), plan)
        self.assertFalse(result['document_collection_authorized'])
        self.assertEqual(result['remote_mutations'], 0)
        class DjvuStore:
            def read_bytes(self, bucket, path):return publication.encode({'files': [{'key': 'book.djvu'}]})
        with patch.object(reset, 'REGISTRIES', []), patch.object(reset, 'is_djvu', return_value=True):
            with self.assertRaisesRegex(ValueError, 'DjVu derived registry'):
                safety.verify_document_reset_prerequisites(DjvuStore(), plan)

    def test_control_transition_is_exact_and_idempotent(self):
        self.assertEqual(safety.enforce_transition('path', b'old', b'old', b'new'), 'ready')
        self.assertEqual(safety.enforce_transition('path', b'new', b'old', b'new'), 'already-applied')
        self.assertEqual(safety.enforce_transition('new', None, None, b'new'), 'ready')
        with self.assertRaisesRegex(ValueError, 'outside the exact plan'):
            safety.enforce_transition('path', b'foreign', b'old', b'new')
        with self.assertRaisesRegex(ValueError, 'unexpectedly exists'):
            safety.enforce_transition('new', b'foreign', None, b'new')

    def test_compare_before_write_rejects_stale_s3_content(self):
        objects = {'control': b'old'}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self, bucket, path):
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
        expected = safety.chm.digest(b'old')
        self.assertTrue(safety.compare_before_write(Store(), 'control', expected, b'new'))
        objects['control'] = b'new'
        self.assertFalse(safety.compare_before_write(Store(), 'control', expected, b'new'))
        objects['control'] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'changed after planning'):
            safety.compare_before_write(Store(), 'control', expected, b'new')

    def test_exact_controls_are_idempotent_and_reject_foreign_paths(self):
        objects = {'reader-index/a.json': b'old'}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self, bucket, path):
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
            def put_bytes(self, bucket, path, raw):objects[path] = raw
        class Fence:
            def check(self):pass
        plan = {'overrides': {'reader-index/a.json': b'new'}}
        before = {'reader-index/a.json': {'sha256': safety.chm.digest(b'old')}}
        events = []
        safety.apply_exact_controls(Store(), plan, before, ['reader-index/a.json'], Fence(),
                                    lambda phase, path: events.append((phase, path)))
        self.assertEqual(objects['reader-index/a.json'], b'new')
        safety.apply_exact_controls(Store(), plan, before, ['reader-index/a.json'], Fence(),
                                    lambda phase, path: events.append((phase, path)))
        self.assertEqual(len(events), 2)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            safety.apply_exact_controls(Store(), plan, before, ['documents/pdf/ps/index.json'], Fence(),
                                        lambda phase, path: None)

    def test_publication_order_never_retires_history_before_both_sites(self):
        staged = {'staged_controls': [safety.chm.SIDECAR], 'post_ack_controls': []}
        self.assertTrue(safety.validate_publication_order(self.plan, staged))
        bad = {**staged, 'post_ack_controls': ['reader-index/v3/retirement.json']}
        with self.assertRaisesRegex(ValueError, 'before both surfaces'):
            safety.validate_publication_order(self.plan, bad)
        bad = {**staged, 'delete_started': True}
        with self.assertRaisesRegex(ValueError, 'actual graph'):
            safety.validate_publication_order(self.plan, bad)

    def test_reference_handoff_rejects_loss_of_unrelated_history(self):
        from unittest.mock import patch
        sidecar = {'v': 1, 'f': {'book.pdf': {'p': 'unrelated'}}}
        catalog = {'current': {'generation': 'old'}, 'generations': {'old': {
            'references': [{'bucket': publication.ASSETS, 'path': 'unrelated'}]}}}
        pointer = {'generation': 'old-v3'}
        plan = {'overrides': {safety.chm.SIDECAR: gzip.compress(publication.encode(sidecar)),
                              safety.chm.CATALOG: publication.encode(copy.deepcopy(catalog))},
                'v3_pointer_before': pointer, 'current_v3_generation': 'old-v3'}
        before = {safety.chm.SIDECAR: gzip.compress(publication.encode(sidecar)),
                  safety.chm.CATALOG: publication.encode(catalog)}
        with patch.object(safety, 'verify_prepared_chapter_retention'):
            self.assertTrue(safety.validate_reference_handoff(before, plan, []))
            changed = copy.deepcopy(catalog)
            changed['generations']['old']['references'] = []
            plan['overrides'][safety.chm.CATALOG] = publication.encode(changed)
            with self.assertRaisesRegex(ValueError, 'unrelated resource'):
                safety.validate_reference_handoff(before, plan, [])

    def test_index_transition_recomputes_each_chapter_route(self):
        from scripts import chm_epub_withdrawal_preview as preview
        source = 'a' * 64
        root = 'chapters/ebook/chm/' + source + '/' + 'b' * 16
        manifest = root + '/chapter-manifest.json'
        fallback = 'native/ebook/chm/' + source + '/document.epub'
        key = 'repo\0book.chm'
        entry = {'key': key, 'source_sha256': source, 'bucket': publication.ASSETS, 'mode': 'chapter-stream',
                 'root': root, 'manifest': manifest, 'search_index': root + '/epub-search-index.json.gz',
                 'chapter_count': 1, 'fallback': fallback, 'object': fallback}
        index = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [entry], 'failures': []}
        sidecar = {'v': 1, 'f': {key: {'s': 2, 'm': 'e', 'p': manifest, 'b': publication.ASSETS,
                                      'c': manifest, 'cb': publication.ASSETS, 'f': fallback}}}
        indexes = {safety.chm.INDEX: index, **{f'{safety.chm.INDEX_PREFIX}index-0{i}.json': copy.deepcopy(index)
                                              for i in range(8)}}
        catalog, _ = publication.reader_lifecycle.catalog_generation(sidecar)
        rows = [{'path': fallback}]
        result = safety.chm.synchronize_indexes(indexes, sidecar, catalog, rows, set(),
                            current_generation='g', receipts={'hf': 'g', 'pages': 'g'})
        before = {path: publication.encode(value) for path, value in indexes.items()}
        planned = {path: publication.encode(value) for path, value in result['indexes'].items()}
        self.assertEqual(safety.validate_chm_index_transition(before, planned, rows, sidecar,
              missing_processing=set(), generation='g', acknowledged_generations={'hf': 'g', 'pages': 'g'})
              ['indexes_verified'], 9)
        bad = json.loads(planned[safety.chm.INDEX])
        bad['files'][0]['manifest'] = 'foreign'
        planned[safety.chm.INDEX] = publication.encode(bad)
        with self.assertRaisesRegex(ValueError, 'unapproved fields'):
            safety.validate_chm_index_transition(before, planned, rows, sidecar,
                 missing_processing=set(), generation='g', acknowledged_generations={'hf': 'g', 'pages': 'g'})

    def test_pre_ack_catalog_preserves_original_epub_refs_and_has_no_false_ack(self):
        from unittest.mock import patch
        old = {'version': 1, 'kind': 'reader-catalog', 'current': {'generation': 'old'},
               'generations': {'old': {'generation': 'old', 'status': 'current',
                                       'references': [{'bucket': publication.ASSETS, 'path': 'old.epub'}]}}}
        new = copy.deepcopy(old)
        new['current'] = {'generation': 'new'}
        new['generations']['old'].update(status='superseded', superseded_at='2026-10-30T00:00:00Z', references=[])
        new['generations']['new'] = {'generation': 'new', 'status': 'current', 'acks': {'hf': True, 'pages': True}}
        pointer = {'generation': 'new'}
        plan = {'v3_pointer_before': {'generation': 'old-v3'}, 'new_v3_generation': 'new', 'base_generation': 'new',
                'immutable': {}, 'overrides': {safety.chm.SIDECAR: b'sidecar', safety.chm.CATALOG: publication.encode(new),
                'reader-index/v3/current.json': publication.encode(pointer),
                'reader-index/v3/promotions/new.json': publication.encode({'acks': {'hf': True, 'pages': True}}),
                'reader-index/chm-epub-withdrawal-retention.json': b'retention'}}
        objects = {safety.chm.CATALOG: publication.encode(old), safety.chm.SIDECAR: b'old-sidecar',
                   'reader-index/v3/current.json': publication.encode(plan['v3_pointer_before'])}
        before = {path: {'present': path in objects, 'sha256': safety.chm.digest(objects[path]) if path in objects else None}
                  for path in plan['overrides']}
        before[safety.chm.CATALOG]['raw'] = publication.encode(old).decode()
        class Store:
            def read_bytes(self, bucket, path):
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
            def put_bytes(self, bucket, path, raw):objects[path] = raw
        class Fence:
            def check(self):pass
        def current(store):return json.loads(objects['reader-index/v3/current.json']), {}
        with patch.object(publication, 'current', side_effect=current):
            staged = safety.apply_pre_ack_publication(Store(), plan, before, Fence(), lambda *_: None)
        staged_catalog = json.loads(staged[safety.chm.CATALOG])
        self.assertEqual(staged_catalog['generations']['old']['references'], old['generations']['old']['references'])
        self.assertEqual(staged_catalog['generations']['new']['acks'], {'hf': False, 'pages': False})
        self.assertEqual(json.loads(staged['reader-index/v3/promotions/new.json'])['acks'], {'hf': False, 'pages': False})

    def test_pre_ack_keeps_each_unacknowledged_base_history_exact(self):
        old={'current':{'generation':'old'},'generations':{
            'old':{'generation':'old','status':'current','references':[{'path':'current.epub'}]},
            'older':{'generation':'older','status':'superseded','references':[{'path':'older.epub'}],
                     'snapshot':{'path':'original'}}}}
        new=copy.deepcopy(old);new['current']={'generation':'new'}
        new['generations']['old'].update(status='superseded',superseded_at='today',references=[])
        new['generations']['older'].update(references=[],snapshot={'path':'cleaned'})
        new['generations']['new']={'generation':'new','status':'current','acks':{'hf':True,'pages':True}}
        plan={'base_generation':'new','new_v3_generation':'new','overrides':{
            safety.chm.SIDECAR:b'sidecar',safety.chm.CATALOG:publication.encode(new),
            'reader-index/v3/current.json':b'{}','reader-index/v3/promotions/new.json':b'{"acks":{}}',
            'reader-index/chm-epub-withdrawal-retention.json':b'{}'}}
        before={safety.chm.CATALOG:{'raw':publication.encode(old).decode()}}
        staged=json.loads(safety.staged_pre_ack_controls(plan,before)[safety.chm.CATALOG])
        self.assertEqual(staged['generations']['older']['references'],old['generations']['older']['references'])
        self.assertEqual(staged['generations']['older']['snapshot'],old['generations']['older']['snapshot'])
        self.assertEqual(staged['generations']['old']['references'],old['generations']['old']['references'])

    def test_pages_dispatch_runs_after_pointer_readback_and_before_pointer_checkpoint(self):
        from unittest.mock import patch
        old={'current':{'generation':'old'},'generations':{'old':{'generation':'old','status':'current'}}}
        new={'current':{'generation':'new'},'generations':{
            'old':{'generation':'old','status':'superseded','superseded_at':'2026-10-30T00:00:00Z'},
            'new':{'generation':'new','status':'current'}}}
        pointer_path='reader-index/v3/current.json';pointer={'generation':'new'}
        plan={'v3_pointer_before':{'generation':'old-v3'},'new_v3_generation':'new','base_generation':'new',
              'immutable':{},'overrides':{safety.chm.SIDECAR:b'sidecar',safety.chm.CATALOG:publication.encode(new),
                pointer_path:publication.encode(pointer),
                'reader-index/v3/promotions/new.json':publication.encode({'acks':{'hf':True,'pages':True}}),
                'reader-index/chm-epub-withdrawal-retention.json':b'retention'}}
        objects={safety.chm.CATALOG:publication.encode(old),safety.chm.SIDECAR:b'old',
                 pointer_path:publication.encode(plan['v3_pointer_before'])}
        before={path:{'present':path in objects,'sha256':safety.chm.digest(objects[path]) if path in objects else None}
                for path in plan['overrides']}
        before[safety.chm.CATALOG]['raw']=publication.encode(old).decode();events=[]
        class Store:
            def read_bytes(self,bucket,path):
                if path not in objects:raise FileNotFoundError(path)
                if path==pointer_path:events.append('pointer-read')
                return objects[path]
            def put_bytes(self,bucket,path,raw):
                if path==pointer_path:events.append('pointer-write')
                objects[path]=raw
        class Fence:
            def check(self):pass
        with patch.object(publication,'current',side_effect=lambda _: (json.loads(objects[pointer_path]),{})):
            safety.apply_pre_ack_publication(Store(),plan,before,Fence(),
                lambda phase,path:events.append('pointer-checkpoint') if path==pointer_path else None,
                on_pointer_committed=lambda:events.append('pages-dispatch'))
        self.assertLess(events.index('pointer-write'),events.index('pages-dispatch'))
        self.assertLess(events.index('pages-dispatch'),events.index('pointer-checkpoint'))

    def test_acknowledgment_writes_are_blocked_until_both_surfaces_accept(self):
        from unittest.mock import patch
        bad = copy.deepcopy(self.reports)
        bad['pages']['active'] = False
        with patch.object(safety, 'observe_planned_projection', return_value=bad):
            with self.assertRaisesRegex(ValueError, 'has not accepted'):
                safety.apply_acknowledged_controls(None, self.plan, {}, {}, None, None, lambda *_: None)

    def test_processing_root_narrowing_requires_live_acceptance_and_retains_chapters(self):
        from unittest.mock import patch
        source='a'*64;owner='reader-index/processing/stream-upload-'+'b'*32+'.json'
        target='native/ebook/chm/'+source
        chapter='chapters/ebook/chm/'+'c'*64+'/'+'d'*16
        before=publication.encode({'status':'processing','resources':[
            {'bucket':publication.ASSETS,'root':chapter},{'bucket':publication.ASSETS,'root':target}]})
        sha=safety.chm.digest(before)
        after=json.loads(before);after['resources']=after['resources'][:1]
        after['withdrawal']={'kind':'explicit-chm-epub-withdrawal',
            'inventory_sha256':safety.chm.preview.INVENTORY_SHA256,'original_sha256':sha,'removed_root_count':1}
        item={'path':owner,'sha256':sha,'target_root':target,'chapter_root':chapter}
        inventory={'records':[item]}
        update={owner:{'before_sha256':sha,'before_raw':before.decode(),'after':publication.encode(after)}}
        rows=[{'path':target+'/document.epub'}]
        objects={owner:before};events=[]
        class Store:
            def list_files(self,bucket,prefixes):return {target+'/document.epub'}
            def read_bytes(self,bucket,path):return objects[path]
            def put_bytes(self,bucket,path,raw):events.append('write');objects[path]=raw
        class Fence:
            api=None
            def check(self):events.append('fence')
        with patch.object(safety.chm,'processing_protection_inventory',return_value=inventory), \
             patch.object(safety.chm,'verify_processing_owner_run'), \
             patch.object(safety,'observe_planned_projection',side_effect=lambda *_:events.append('acceptance')):
            safety.apply_processing_protection_updates(Store(),rows,Fence(),None,self.plan,update,
                                                       lambda phase,path:events.append(phase))
            self.assertLess(events.index('acceptance'),events.index('processing-update-submitted'))
            self.assertLess(events.index('processing-update-submitted'),events.index('write'))
            self.assertEqual(json.loads(objects[owner])['resources'],[{'bucket':publication.ASSETS,'root':chapter}])
            events.clear()
            safety.apply_processing_protection_updates(Store(),rows,Fence(),None,self.plan,update,lambda *_:None)
            self.assertNotIn('write',events)
            objects[owner]=before
            altered=copy.deepcopy(update);bad=json.loads(altered[owner]['after']);bad['resources']=[]
            altered[owner]['after']=publication.encode(bad)
            with self.assertRaisesRegex(ValueError,'protected chapter'):
                safety.apply_processing_protection_updates(Store(),rows,Fence(),None,self.plan,altered,lambda *_:None)
            with patch.object(safety,'observe_planned_projection',side_effect=ValueError('not accepted')):
                with self.assertRaisesRegex(ValueError,'not accepted'):
                    safety.apply_processing_protection_updates(Store(),rows,Fence(),None,self.plan,update,lambda *_:None)
            self.assertEqual(objects[owner],before)

    def test_guarded_runner_never_trusts_simulation_with_retained_targets(self):
        from unittest.mock import patch
        with patch.object(safety, 'validate_staged_plan'):
            with self.assertRaisesRegex(ValueError, 'unresolved references'):
                safety.run_guarded_retirement(None, None, None, self.plan, [],
                    preflight={'graph_complete': True, 'retained_targets': 1, 'target_count': 0,
                               'remote_mutations': 0, 'deletion_authorized': False},
                    persist=lambda *_: None, save_plan=lambda *_: None, publish_pages=lambda *_: None,
                    chapter_evidence={}, processing_updates={})

    def test_staged_plan_rejects_any_foreign_control(self):
        from unittest.mock import patch
        plan = copy.deepcopy(self.plan)
        plan['kind'] = 'chm-epub-withdrawal-plan'
        plan['overrides'].update({p: b'{}' for p in [safety.chm.SIDECAR, safety.chm.CATALOG,
            'reader-index/v3/retirement.json', 'reader-index/reader_gc_consumers.json',
            'reader-index/chm-epub-withdrawal-retention.json', safety.chm.INDEX,
            *(f'{safety.chm.INDEX_PREFIX}index-0{i}.json' for i in range(8)),
            f"reader-index/v3/promotions/{plan['new_v3_generation']}.json"]})
        with patch.object(safety, 'require_exact_epub_inventory'):
            self.assertTrue(safety.validate_staged_plan(plan, []))
            plan['overrides']['documents/pdf/ps/index.json'] = b'{}'
            with self.assertRaisesRegex(ValueError, 'unexpected control'):
                safety.validate_staged_plan(plan, [])

    def test_preserved_catalog_alias_checks_original_overrides_and_epubs(self):
        old = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'parent_generation': 'd' * 64}
        old['generation'] = publication.text.digest(old)
        base = {'v': 1, 'f': {'book.chm': {'p': 'chapter'}}}
        packed = gzip.compress(publication.encode(base), mtime=0)
        base_ref = publication.metadata(publication.ASSETS,
                   f'reader-index/snapshots/{safety.chm.digest(packed)}/reader_assets.json.gz', packed)
        new = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'base_sidecar': base_ref,
               'parent_generation': 'd' * 64, 'rollback_target': old['generation']}
        new['generation'] = publication.text.digest(new)
        raw = publication.encode(new)
        alias = publication.metadata(publication.ASSETS,
                    f"reader-index/v3/generations/{new['generation']}/catalog.json", raw)
        retired = [f"reader-index/v3/generations/{old['generation']}/catalog.json",
                   f"reader-index/v3/promotions/{old['generation']}.json"]
        plan = {'rollback_aliases': {old['generation']: alias},
                'overrides': {'reader-index/v3/retirement.json': publication.encode({'resources': [alias],
                    'retired_paths': retired, 'rollback_replacements': {old['generation']: alias}})},
                'immutable': {alias['path']: raw, base_ref['path']: packed}}
        rows = [{'path': 'native/ebook/chm/target/document.epub'}]
        self.assertTrue(safety.validate_preserved_catalog_roots(plan, {old['generation']: old}, rows))
        plan['immutable'][alias['path']] = b'changed'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            safety.validate_preserved_catalog_roots(plan, {old['generation']: old}, rows)

    def test_saved_plan_loads_every_history_alias(self):
        from unittest.mock import patch
        generations=['a'*64,'b'*64,'c'*64]
        plan={'current_v3_generation':generations[2],'old_v3_generation':generations[0],
              'history_generations':generations,'rollback_aliases':{g:{} for g in generations}}
        class Store:
            def read_bytes(self,bucket,path):return json.dumps({'generation':path.split('/')[-2]}).encode()
        with patch.object(publication,'validate_catalog',side_effect=lambda value:value):
            self.assertEqual(set(safety.load_plan_history_catalogs(Store(),plan)),set(generations))
            altered=copy.deepcopy(plan);altered['history_generations'].remove(generations[1])
            with self.assertRaisesRegex(ValueError,'every rollback alias'):
                safety.load_plan_history_catalogs(Store(),altered)

    def test_historical_base_must_preserve_every_non_chm_route(self):
        from unittest.mock import patch
        base = {'v': 1, 'f': {'book.pdf': {'p': 'original'}}}
        packed = gzip.compress(publication.encode(base), mtime=0)
        replacement = {'base_sidecar': {'path': 'snapshot'}, 'generation': 'new'}
        plan = {'rollback_aliases': {'old': {'path': 'catalog'}},
                'immutable': {'catalog': publication.encode(replacement), 'snapshot': packed}}
        with patch.object(publication, 'pinned_base', return_value=base), \
                patch.object(publication, 'validate_catalog', side_effect=lambda value: value):
            self.assertTrue(safety.validate_old_base_routes(None, plan, {'old': {}}, []))
            altered = {'v': 1, 'f': {'book.pdf': {'p': 'changed'}}}
            plan['immutable']['snapshot'] = gzip.compress(publication.encode(altered), mtime=0)
            with self.assertRaisesRegex(ValueError, 'preserved historical routes'):
                safety.validate_old_base_routes(None, plan, {'old': {}}, [])

    def test_book_originals_and_chapter_resource_inventories_cannot_change(self):
        root = 'chapters/ebook/chm/' + 'a' * 64 + '/' + 'b' * 16
        before = {('dataset', 'source.chm'): ('sha', 7),
                  (publication.ASSETS, root + '/chapter-manifest.json'): ('manifest-sha', 5)}
        self.assertEqual(safety.verify_originals_and_chapter_retention(before, dict(before), {root})
                         ['unrelated_objects_verified'], 2)
        after = dict(before)
        after[('dataset', 'source.chm')] = ('changed', 7)
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            safety.verify_originals_and_chapter_retention(before, after, {root})

    def test_retention_root_must_pin_every_historical_chapter(self):
        path = 'chapters/ebook/chm/' + 'a' * 64 + '/' + 'b' * 16 + '/chapter-manifest.json'
        retention = {'kind': 'reader-chm-epub-withdrawal-retention', 'inventory_sha256': 'abc',
                     'resources': [{'bucket': publication.ASSETS, 'path': path}]}
        plan = {'inventory_sha256': 'abc', 'chapter_manifests_to_preserve': [path],
                'overrides': {'reader-index/chm-epub-withdrawal-retention.json': publication.encode(retention)}}
        self.assertEqual(safety.verify_prepared_chapter_retention(plan)['protected_chapter_manifests'], 1)
        retention['resources'] = []
        plan['overrides']['reader-index/chm-epub-withdrawal-retention.json'] = publication.encode(retention)
        with self.assertRaisesRegex(ValueError, 'omits'):
            safety.verify_prepared_chapter_retention(plan)

    def test_chapter_evidence_requires_exact_manifests_hashes_and_counts(self):
        paths = [f'chapters/ebook/chm/{c * 64}/' + 'b' * 16 + '/chapter-manifest.json' for c in 'ac']
        refs = [{'bucket': publication.ASSETS, 'path': path, 'sha256': c * 64}
                for path, c in zip(paths, 'de')]
        root = {'kind':'reader-chm-epub-withdrawal-retention', 'inventory_sha256':'abc', 'resources':refs}
        plan = {'inventory_sha256':'abc', 'chapter_manifests_to_preserve':paths,
                'overrides':{'reader-index/chm-epub-withdrawal-retention.json':publication.encode(root)}}
        results = [{'manifest':ref['path'], 'manifest_sha256':ref['sha256'], 'required':3,
                    'bundle_paths_sha256':'a'*64,
                    'other_resources':2, 'bundle_objects':5, 'remote_mutations':0} for ref in refs]
        evidence = {'manifests':2, 'results':results, 'required_objects':6,
                    'companion_objects':4, 'remote_mutations':0}
        self.assertEqual(safety.require_chapter_evidence(plan, evidence)['manifests'], 2)
        for name, mutate in [
            ('wrong digest', lambda v: v['results'][0].update(manifest_sha256='f' * 64)),
            ('duplicate', lambda v: v['results'].__setitem__(1, copy.deepcopy(v['results'][0]))),
            ('wrong bundle count', lambda v: v['results'][0].update(bundle_objects=4)),
            ('wrong total', lambda v: v.update(required_objects=5)),
            ('mutated', lambda v: v['results'][0].update(remote_mutations=1)),
        ]:
            bad=copy.deepcopy(evidence);mutate(bad)
            with self.subTest(name=name), self.assertRaises(ValueError):
                safety.require_chapter_evidence(plan, bad)

    def test_hypothetical_graph_and_foreign_batch_never_authorize_delete(self):
        rows = [{'bucket': publication.ASSETS, 'path': 'native/ebook/chm/reviewed/document.epub'}]
        proof = {'graph_complete': True, 'mode': 'simulated', 'target_count': 1, 'retained_targets': 0,
                 'origins': [], 'ambiguous_paths': [], 'blockers': [], 'root_recheck_complete': True,
                 'reader_acceptance_complete': True}
        with self.assertRaisesRegex(ValueError, 'actual CHM graph'):
            safety.require_actual_zero_graph(proof, rows)
        proof['mode'] = 'actual'
        safety.require_actual_zero_graph(proof, rows)
        self.assertTrue(safety.validate_delete_batch(rows, [(publication.ASSETS, rows[0]['path'])]))
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            safety.validate_delete_batch(rows, [(publication.ASSETS, 'source.chm')])

    def test_exact_inventory_rejects_same_size_foreign_key_or_changed_hash(self):
        from pathlib import Path
        from scripts import chm_epub_withdrawal_preview as preview
        inventory=preview.load_inventory(Path(__file__).resolve().parents[1] /
                                         'config/chm-history-inventory-20261030.json')
        rows=inventory['targets']
        plan={'inventory_sha256':preview.INVENTORY_SHA256,'target_count':len(rows),
              'target_bytes':inventory['logical_bytes']}
        safety.require_exact_epub_inventory(plan,rows)
        for field,value in [('path', 'native/ebook/chm/' + 'f' * 64 + '/document.epub'),
                            ('sha256', 'f' * 64)]:
            altered=copy.deepcopy(rows);altered[0][field]=value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'hashed reviewed inventory'):
                safety.require_exact_epub_inventory(plan,altered)

    def test_actual_graph_proof_requires_fresh_root_recheck(self):
        from unittest.mock import patch
        rows = [{'bucket': publication.ASSETS, 'path': 'native/ebook/chm/reviewed/document.epub'}]
        class Store:
            def assert_buckets_available(self):pass
        class Fence:
            def check(self):pass
        class FakeGraph:
            def __init__(self, store, **kwargs):
                self.references = set()
                self.direct = {}
                from scripts import reader_gc_graph
                self.files = {bucket: set() for bucket in reader_gc_graph.BUCKETS}
                self.root_hashes = {}
                self.loaded_digests = {}
                self.retired_roots = set()
            def build(self):return {'graph_complete': True, 'blockers': [], 'ambiguous_paths': [], 'roots': []}
        with patch('scripts.reader_gc_graph.ReferenceGraph', FakeGraph), \
                patch('scripts.reader_gc_collect.verify_roots') as roots, \
                patch.object(safety, 'observe_planned_projection', return_value=self.reports) as observed:
            proof, _ = safety.actual_graph_evidence(Store(), Fence(), None, rows, self.plan)
        self.assertEqual(proof['mode'], 'actual')
        self.assertEqual(proof['retained_targets'], 0)
        self.assertEqual(roots.call_count, 1)
        self.assertEqual(observed.call_count, 2)
        safety.require_actual_zero_graph(proof, rows)

    def test_predelete_barrier_rejects_changed_root(self):
        from unittest.mock import patch
        rows = [{'bucket': publication.ASSETS, 'path': 'native/ebook/chm/reviewed/document.epub'}]
        proof = {'graph_complete': True, 'mode': 'actual', 'target_count': 1, 'retained_targets': 0,
                 'origins': [], 'root_recheck_complete': True, 'reader_acceptance_complete': True,
                 'root_sha256': [{'bucket': publication.ASSETS, 'path': 'reader-index/root.json',
                                  'sha256': 'a' * 64}],
                 'closure_sha256': [{'bucket': publication.ASSETS, 'path': 'reader-index/root.json',
                                     'sha256': 'a' * 64}],
                 'root_members': [{'bucket': publication.ASSETS, 'path': 'reader-index/root.json'}],
                 'retired_roots': []}
        class Store:
            def assert_buckets_available(self):pass
            def digest(self, bucket, path):return 'b' * 64
        class Fence:
            def check(self):pass
        with patch('scripts.reader_gc_collect.verify_roots'), patch.object(safety, 'observe_planned_projection'):
            with self.assertRaisesRegex(ValueError, 'graph document changed'):
                safety.recheck_actual_graph(Store(), None, proof, rows, Fence(), None, self.plan)

    def test_predelete_barrier_checks_non_root_graph_documents(self):
        from unittest.mock import patch
        rows = [{'bucket': publication.ASSETS, 'path': 'native/ebook/chm/reviewed/document.epub'}]
        root={'bucket':publication.ASSETS,'path':'reader-index/root.json','sha256':'a'*64}
        nested={'bucket':publication.ASSETS,'path':'reader-index/snapshots/nested.json','sha256':'b'*64}
        proof={'graph_complete':True,'mode':'actual','target_count':1,'retained_targets':0,'origins':[],
               'root_recheck_complete':True,'reader_acceptance_complete':True,'root_sha256':[root],
               'root_members':[{'bucket':root['bucket'],'path':root['path']}], 'retired_roots':[],
               'closure_sha256':[root,nested]}
        class Store:
            def assert_buckets_available(self):pass
            def digest(self, bucket, path):return 'a'*64 if path == root['path'] else 'c'*64
        class Fence:
            def check(self):pass
        with patch('scripts.reader_gc_collect.verify_roots'), patch.object(safety,'observe_planned_projection'):
            with self.assertRaisesRegex(ValueError,'graph document changed'):
                safety.recheck_actual_graph(Store(),None,proof,rows,Fence(),None,self.plan)

    def test_simulation_barrier_rejects_changed_nonoverlay_dependency(self):
        from unittest.mock import patch
        ref=(publication.ASSETS,'reader-index/snapshots/nested.json')
        class Graph:
            store=None
            loaded_digests={ref:'a'*64}
        class Store:
            def assert_buckets_available(self):pass
            def digest(self,*args):return 'b'*64
        class Fence:
            def check(self):pass
        details={'graph':Graph(),'live_control_sha256':{},'overlay_paths':set()}
        with patch('scripts.reader_gc_collect.verify_roots'):
            with self.assertRaisesRegex(ValueError,'live input changed'):
                safety.recheck_simulation_inputs(Store(),details,Fence())

    def test_simulation_barrier_after_staging_handles_not_yet_written_roots(self):
        from scripts import reader_gc_graph
        bucket=publication.ASSETS;root='reader-index/old.json';staged='reader-index/new.json';later='reader-index/later.json'
        immutable='reader-index/v3/generations/'+'a'*64+'/catalog.json'
        class Graph:
            files={b:set() for b in reader_gc_graph.BUCKETS}
            root_names={b:set() for b in reader_gc_graph.BUCKETS}
            loaded_digests={}
        graph=Graph();graph.files[bucket]={root,staged,later,immutable}
        graph.root_names[bucket]=set(graph.files[bucket])
        objects={root:b'old',staged:b'new',immutable:b'catalog'}
        class Store:
            def assert_buckets_available(self):pass
            def list_files(self,b,prefixes):return set()
            def list_roots(self,b,parents):return {p:None for p in objects} if b==bucket else {}
            def digest(self,b,p):
                if p not in objects:raise FileNotFoundError(p)
                return safety.chm.digest(objects[p])
        class Fence:
            def check(self):pass
        details={'graph':graph,'overlay_paths':{(bucket,p) for p in (staged,later,immutable)},
                 'live_control_sha256':{(bucket,staged):None,(bucket,later):None,(bucket,immutable):None}}
        self.assertTrue(safety.recheck_simulation_inputs(Store(),details,Fence(),
                                                       applied={staged:b'new',immutable:b'catalog'}))
        objects[later]=b'unapproved'
        with self.assertRaisesRegex(ValueError,'root inventory changed'):
            safety.recheck_simulation_inputs(Store(),details,Fence(),
                                             applied={staged:b'new',immutable:b'catalog'})

    def test_live_controls_must_equal_the_exact_approved_plan(self):
        plan = {'overrides': {'reader-index/a.json': b'planned'},
                'immutable': {'reader-index/snapshots/x': b'snapshot'}}
        objects = {**plan['overrides'], **plan['immutable']}
        class Store:
            def assert_buckets_available(self):pass
            def read_bytes(self, bucket, path):return objects[path]
        self.assertTrue(safety.verify_live_chm_controls(Store(), plan))
        objects['reader-index/a.json'] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'differs from the approved plan'):
            safety.verify_live_chm_controls(Store(), plan)

    def test_live_failed_upload_controls_and_rollback_aliases_are_rechecked(self):
        from unittest.mock import patch
        updates={'reader-index/processing/exact.json':{'after':b'expected'}}
        old='a'*64;alias={'path':'reader-index/v3/generations/'+'b'*64+'/catalog.json'}
        objects={'reader-index/processing/exact.json':b'expected',
                 f'reader-index/v3/generations/{old}/catalog.json':b'old'}
        class Store:
            def assert_buckets_available(self):pass
            def read_bytes(self,bucket,path):return objects[path]
        self.assertTrue(safety.verify_live_processing_controls(Store(),updates))
        with patch.object(safety.chm,'resolve_retired_rollback',return_value=alias):
            self.assertTrue(safety.verify_retired_rollback_aliases(Store(),{'rollback_aliases':{old:alias}}))
        with patch.object(safety.chm,'resolve_retired_rollback',return_value={'path':'wrong'}):
            with self.assertRaisesRegex(ValueError,'does not resolve'):
                safety.verify_retired_rollback_aliases(Store(),{'rollback_aliases':{old:alias}})
        objects['reader-index/processing/exact.json']=b'changed'
        with self.assertRaisesRegex(ValueError,'differs'):
            safety.verify_live_processing_controls(Store(),updates)

    def test_preserved_chapter_paths_are_checked_before_and_after_deletion(self):
        root='chapters/ebook/chm/'+'a'*64+'/'+'b'*16
        files={root+'/chapter-manifest.json',root+'/chapter.xhtml',root+'/resources/font.ttf'}
        proof={'chapter_bundles':[{'root':root,'count':len(files),
            'paths_sha256':safety.chm.digest(publication.encode(sorted(files)))}]}
        updates={'reader-index/processing/owner.json':{'after':b'kept'}}
        class Store:
            def list_files(self,bucket,prefixes):return set(files)
            def read_bytes(self,bucket,path):return b'kept'
        self.assertTrue(safety.verify_predelete_chapter_inventory(Store(),proof,updates))
        files.remove(root+'/resources/font.ttf')
        with self.assertRaisesRegex(ValueError,'chapter bundle inventory changed'):
            safety.verify_predelete_chapter_inventory(Store(),proof,updates)

    def test_chapter_inventory_uses_fresh_full_listing_without_skipping_any_bundle(self):
        roots=['chapters/ebook/chm/'+source+'/'+'f'*16 for source in
               ('01'+'a'*62,'01'+'b'*62,'02'+'c'*62)]
        files={root+'/chapter-manifest.json' for root in roots} | {root+'/chapters/body.xhtml' for root in roots}
        rows=[{'root':root,'count':2,'paths_sha256':safety.chm.digest(publication.encode(
               sorted(path for path in files if path.startswith(root+'/'))))} for root in roots]
        prefixes=[]
        class Store:
            def list_files(self,bucket,values):
                prefixes.extend(values)
                return {path for path in files if path.startswith(values)}
        self.assertTrue(safety.require_initial_chapter_inventory(Store(),rows))
        self.assertEqual(prefixes,['chapters/ebook/chm/'])
        files.remove(roots[1]+'/chapters/body.xhtml')
        with self.assertRaisesRegex(ValueError,'chapter bundle inventory changed'):
            safety.require_initial_chapter_inventory(Store(),rows)

    def test_exact_chm_recovery_requires_a_durable_submitted_batch(self):
        data = b'epub'
        row = {'bucket': publication.ASSETS, 'path': 'native/ebook/chm/x/document.epub',
               'bytes': len(data), 'sha256': safety.chm.digest(data)}
        objects = {row['path']: data}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self, bucket, path):
                if path not in objects:
                    raise FileNotFoundError(path)
                return objects[path]
        self.assertEqual(safety.verify_exact_epub_targets(Store(), [row])['verified_objects'], 1)
        objects.pop(row['path'])
        with self.assertRaisesRegex(ValueError, 'unsubmitted'):
            safety.reconcile_epub_deletions(Store(), [row], [])
        receipt = [{'bucket': row['bucket'], 'paths': [row['path']], 'status': 'submitted'}]
        self.assertEqual(safety.reconcile_epub_deletions(Store(), [row], receipt)['confirmed_absent_objects'], 1)
        with self.assertRaisesRegex(RuntimeError, 'availability'):
            safety.reconcile_epub_deletions(object(), [row], receipt)
        objects[row['path']]=data
        receipt[0]['status']='verified-absent'
        with self.assertRaisesRegex(ValueError, 'republished'):
            safety.reconcile_epub_deletions(Store(), [row], receipt)

    def test_chm_delete_batches_log_before_writing_and_recover_partial_success(self):
        data = [b'one', b'two']
        rows = [{'bucket': publication.ASSETS, 'path': f'native/ebook/chm/{i}/document.epub',
                 'bytes': len(raw), 'sha256': safety.chm.digest(raw)} for i, raw in enumerate(data)]
        objects = {row['path']: raw for row, raw in zip(rows, data)}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self, bucket, path):
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
            def delete(self, bucket, paths):
                self.asserted = list(paths)
                for path in paths:objects.pop(path, None)
        class Fence:
            def check(self):pass
        receipt = [{'bucket': publication.ASSETS, 'paths': [row['path'] for row in rows],
                    'status': 'submitted'}]
        objects.pop(rows[0]['path'])
        events = []
        batches, result = safety.delete_exact_epub_batches(Store(), rows, Fence(),
            lambda phase, batches, status: events.append((phase, copy.deepcopy(batches))),
            barrier=lambda: events.append(('actual-barrier', None)), batches=receipt)
        self.assertEqual(result['confirmed_absent_objects'], 2)
        self.assertEqual(batches[0]['status'], 'verified-absent')
        self.assertLess([x[0] for x in events].index('batch-submitted'),
                        [x[0] for x in events].index('actual-barrier'))
        self.assertLess([x[0] for x in events].index('actual-barrier'),
                        [x[0] for x in events].index('batch-verified'))

    def test_failed_batch_barrier_never_calls_delete(self):
        row={'bucket': publication.ASSETS,'path':'native/ebook/chm/a/document.epub',
             'bytes':3,'sha256':safety.chm.digest(b'one')}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self, bucket, path):return b'one'
            def delete(self, bucket, paths):raise AssertionError('must not delete')
        class Fence:
            def check(self):pass
        def blocked():raise ValueError('consumer moved')
        with self.assertRaisesRegex(ValueError, 'consumer moved'):
            safety.delete_exact_epub_batches(Store(), [row], Fence(), lambda *_: None, barrier=blocked)

    def test_target_changed_or_missing_during_long_barrier_is_never_deleted(self):
        row={'bucket':publication.ASSETS,'path':'native/ebook/chm/a/document.epub',
             'bytes':3,'sha256':safety.chm.digest(b'one')}
        for replacement in (b'two', b'o', None):
            with self.subTest(replacement=replacement):
                objects={row['path']:b'one'}; events=[]
                class Store:
                    def assert_chm_bucket_available(self):return True
                    def read_bytes(self, bucket, path):
                        if path not in objects:raise FileNotFoundError(path)
                        return objects[path]
                    def delete(self, bucket, paths):raise AssertionError('must not delete')
                class Fence:
                    def check(self):pass
                def barrier():
                    if replacement is None:objects.pop(row['path'])
                    else:objects[row['path']]=replacement
                with self.assertRaises((ValueError,FileNotFoundError)):
                    safety.delete_exact_epub_batches(Store(),[row],Fence(),
                        lambda phase,batches,status:events.append((phase,copy.deepcopy(batches))),barrier=barrier)
                self.assertEqual(events[-1][0],'batch-submitted')
                self.assertEqual(events[-1][1][0]['status'],'submitted')

    def test_successful_batch_rechecks_body_after_barrier_and_before_delete(self):
        row={'bucket':publication.ASSETS,'path':'native/ebook/chm/a/document.epub',
             'bytes':3,'sha256':safety.chm.digest(b'one')}
        objects={row['path']:b'one'};events=[]
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self,bucket,path):
                events.append('read')
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
            def delete(self,bucket,paths):
                events.append('delete')
                for path in paths:objects.pop(path)
        class Fence:
            def check(self):pass
        safety.delete_exact_epub_batches(Store(),[row],Fence(),lambda *_:None,
                                         barrier=lambda:events.append('barrier'))
        self.assertLess(events.index('barrier'),events.index('delete'))
        self.assertIn('read',events[events.index('barrier')+1:events.index('delete')])

    def test_bounded_stage_keeps_all_unsubmitted_targets_and_resumes(self):
        rows = [{'bucket': publication.ASSETS, 'path': f'native/ebook/chm/{i}/document.epub',
                 'bytes': 1, 'sha256': safety.chm.digest(b'x')} for i in range(5)]
        objects = {row['path']: b'x' for row in rows}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self,bucket,path):
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
            def delete(self,bucket,paths):
                for path in paths:objects.pop(path)
        class Fence:
            def check(self):pass
        events=[]
        batches,partial=safety.delete_exact_epub_batches(Store(),rows,Fence(),
            lambda phase,batches,status:events.append(phase),barrier=lambda:None,batch_size=2,max_new_batches=1)
        self.assertEqual(partial['confirmed_absent_objects'],2)
        self.assertEqual(len(objects),3)
        self.assertEqual(events[-1],'partial-stage-complete')
        self.assertEqual(len(batches),1)
        batches,final=safety.delete_exact_epub_batches(Store(),rows,Fence(),lambda *_:None,
            barrier=lambda:None,batches=batches,batch_size=2,max_new_batches=2)
        self.assertEqual(final['confirmed_absent_objects'],5)
        self.assertEqual(len(batches),3)
        self.assertTrue(all(batch['status']=='verified-absent' for batch in batches))

    def test_graph_versions_require_every_get_etag_and_reject_changed_or_missing(self):
        refs={(publication.ASSETS,'reader-index/root.json'):'a'*64,
              (publication.ASSETS,'chapters/b/chapter-manifest.json'):'b'*64}
        class Traversal:
            loaded_digests=refs
        class Store:
            read_etags={ref:'"v1"' for ref in refs}
            current={path:'"v1"' for _,path in refs}
            def list_exact_versions(self,bucket,paths):return {p:self.current[p] for p in paths if p in self.current}
        store=Store()
        versions=safety.capture_graph_versions(store,Traversal())
        self.assertEqual(set(versions),set(refs))
        self.assertTrue(safety.recheck_graph_versions(store,versions))
        store.current['chapters/b/chapter-manifest.json']='"changed"'
        with self.assertRaisesRegex(ValueError,'version changed'):
            safety.recheck_graph_versions(store,versions)
        with self.assertRaisesRegex(ValueError,'changed between read'):
            safety.capture_graph_versions(store,Traversal())
        store.current['chapters/b/chapter-manifest.json']='W/"v1"'
        with self.assertRaisesRegex(ValueError,'changed between read'):
            safety.capture_graph_versions(store,Traversal())
        store.current.pop('chapters/b/chapter-manifest.json')
        with self.assertRaisesRegex(ValueError,'incomplete'):
            safety.capture_graph_versions(store,Traversal())

    def test_versioned_batch_barrier_rechecks_all_nested_documents_and_both_sites(self):
        from unittest.mock import patch
        bucket=publication.ASSETS;root=(bucket,'reader-index/root.json');nested=(bucket,'chapters/a/chapter-manifest.json')
        digests={root:'a'*64,nested:'b'*64};versions={root:'"r1"',nested:'"n1"'}
        class Traversal:
            loaded_digests=digests
        class Store:
            def assert_buckets_available(self):pass
            def list_exact_versions(self,bucket,paths):return {p:versions[(bucket,p)] for p in paths}
            def digest(self,bucket,path):return digests[(bucket,path)]
        class Fence:
            def check(self):pass
        proof={'mode':'actual','graph_complete':True,'target_count':1,'retained_targets':0,'origins':[],
               'ambiguous_paths':[],'blockers':[],'root_recheck_complete':True,'reader_acceptance_complete':True,
               'root_members':[{'bucket':root[0],'path':root[1]}],
               'root_sha256':[{'bucket':root[0],'path':root[1],'sha256':digests[root]}],
               'closure_sha256':[{'bucket':b,'path':p,'sha256':d} for (b,p),d in digests.items()],
               'retired_roots':[]}
        rows=[{'bucket':bucket,'path':'native/ebook/chm/exact/document.epub'}]
        with patch('scripts.reader_gc_collect.verify_roots') as rootcheck, \
             patch.object(safety,'recheck_full_root_inventory') as fullroots, \
             patch.object(safety,'observe_planned_projection') as sites:
            self.assertTrue(safety.recheck_actual_graph_by_version(Store(),Traversal(),proof,rows,
                                                                  Fence(),None,{},versions))
            self.assertEqual(rootcheck.call_count,2);fullroots.assert_called_once();sites.assert_called_once()
            versions.pop(nested)
            with self.assertRaisesRegex(ValueError,'complete actual graph closure'):
                safety.recheck_actual_graph_by_version(Store(),Traversal(),proof,rows,Fence(),None,{},versions)

    def test_versioned_barrier_rejects_changed_bytes_even_with_same_etag(self):
        from unittest.mock import Mock, patch
        bucket=publication.ASSETS;root=(bucket,'reader-index/root.json');nested=(bucket,'chapters/a/chapter-manifest.json')
        digests={root:'a'*64,nested:'b'*64};versions={root:'"same"',nested:'"same"'}
        class Traversal:
            loaded_digests=digests
        class Store:
            def assert_buckets_available(self):pass
            def list_exact_versions(self,bucket,paths):return {p:versions[(bucket,p)] for p in paths}
            def digest(self,bucket,path):return 'c'*64 if (bucket,path)==nested else digests[(bucket,path)]
        proof={'mode':'actual','graph_complete':True,'target_count':1,'retained_targets':0,'origins':[],
               'ambiguous_paths':[],'blockers':[],'root_recheck_complete':True,'reader_acceptance_complete':True,
               'root_members':[{'bucket':root[0],'path':root[1]}],
               'root_sha256':[{'bucket':root[0],'path':root[1],'sha256':digests[root]}],
               'closure_sha256':[{'bucket':b,'path':p,'sha256':d} for (b,p),d in digests.items()],
               'retired_roots':[]}
        rows=[{'bucket':bucket,'path':'native/ebook/chm/exact/document.epub'}]
        with patch('scripts.reader_gc_collect.verify_roots'), \
             patch.object(safety,'observe_planned_projection') as sites:
            with self.assertRaisesRegex(ValueError,'SHA-256 changed'):
                safety.recheck_actual_graph_by_version(Store(),Traversal(),proof,rows,Mock(),None,{},versions)
            sites.assert_not_called()

    def test_versioned_simulation_barrier_rejects_changed_bytes_with_same_etag(self):
        from unittest.mock import Mock, patch
        ref=(publication.ASSETS,'chapters/a/chapter-manifest.json')
        class Graph:
            store=None
            loaded_digests={ref:'a'*64}
        class Store:
            def assert_buckets_available(self):pass
            def list_exact_versions(self,bucket,paths):return {p:'"same"' for p in paths}
            def digest(self,bucket,path):return 'b'*64
        details={'graph':Graph(),'overlay_paths':set(),'live_control_sha256':{}}
        with patch('scripts.reader_gc_collect.verify_roots'):
            with self.assertRaisesRegex(ValueError,'SHA-256 changed'):
                safety.recheck_simulation_by_version(Store(),details,Mock(),versions={ref:'"same"'})
        with self.assertRaisesRegex(ValueError,'cannot authorize applied controls'):
            safety.recheck_simulation_by_version(Store(),details,Mock(),versions={ref:'"same"'},
                                                 applied={'reader-index/any.json':b'changed'},pre_ack_only=True)

    def test_full_root_barrier_finds_new_directory_and_planned_missing_overlay(self):
        from scripts import reader_gc_graph as graph
        bucket=publication.ASSETS
        root='chapters/ebook/chm/known/index.json'
        planned='reader-index/planned.json'
        class Traversal:
            files={b:set() for b in graph.BUCKETS}
        traversal=Traversal();traversal.files[bucket]={root,planned}
        current={b:set() for b in graph.BUCKETS};current[bucket]={root}
        class Store:
            root_predicate=None
            def list_files(self,b,prefixes):return set(current[b])
        store=Store()
        self.assertTrue(safety.recheck_full_root_inventory(store,traversal,expected_absent={(bucket,planned)}))
        self.assertIsNone(store.root_predicate)
        current[bucket].add('chapters/ebook/other/new/index.json')
        with self.assertRaisesRegex(ValueError,'complete root inventory changed'):
            safety.recheck_full_root_inventory(store,traversal,expected_absent={(bucket,planned)})

    def test_progress_checkpoint_cannot_escape_narrow_audit_prefix(self):
        objects = {}
        class Store:
            def put_bytes(self, bucket, path, raw):objects[path] = raw
            def read_bytes(self, bucket, path):return objects[path]
        safety.write_exact_progress(Store(), 'reader-index/gc/chm-epub-withdrawal/current.json', {'phase': 'planned'})
        with self.assertRaisesRegex(ValueError, 'escaped'):
            safety.write_exact_progress(Store(), 'reader-index/v3/current.json', {'phase': 'planned'})


if __name__ == '__main__':
    unittest.main()
