import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import chm_epub_journal as journal, chm_epub_publication_safety as safety
from scripts import publish_reader_v3 as publication


class ChmJournalTests(unittest.TestCase):
    def setUp(self):
        self.objects={}
        class Store:
            def assert_buckets_available(self):pass
            def read_bytes(inner,bucket,path):
                if path not in self.objects:raise FileNotFoundError(path)
                return self.objects[path]
            def put_bytes(inner,bucket,path,raw):self.objects[path]=raw
        self.store=Store()
        self.plan={'kind':'chm-epub-withdrawal-plan','inventory_sha256':safety.chm.preview.INVENTORY_SHA256,
                   'overrides':{safety.chm.CATALOG:b'catalog'},'immutable':{'immutable':b'bytes'},
                   'index_hashes_before':{},'sidecar_sha256_before':'a'*64,
                   'catalog_sha256_before':safety.chm.digest(b'old catalog')}
        self.before={safety.chm.CATALOG:{'present':True,'sha256':safety.chm.digest(b'old catalog'),
                     'bytes':11,'raw':'old catalog'},'immutable':{'present':False,'sha256':None,'bytes':None},
                     safety.chm.SIDECAR:{'present':True,'sha256':'a'*64,'bytes':1}}
        self.plan['overrides'][safety.chm.SIDECAR]=b'new'
        self.updates={'owner':{'before_raw':'old','before_sha256':'a'*64,'after':b'new',
                               'chapter':{'manifest':'chapter','bundle_objects':2,'bundle_paths_sha256':'b'*64}}}

    def test_plan_is_immutable_digest_checked_and_round_trips(self):
        inventory={'records':[{'path':'owner'}]}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(safety,'validate_staged_plan'), \
             patch.object(safety.chm,'processing_protection_inventory',return_value=inventory):
            digest=journal.save_plan(self.store,self.plan,self.before,self.updates,Path(directory))
            plan,before,updates,read_digest=journal.load_plan(self.store,[])
            self.assertEqual(read_digest,digest)
            self.assertEqual(plan,self.plan);self.assertEqual(before,self.before);self.assertEqual(updates,self.updates)
            self.objects[journal.PLAN]=b'changed'
            with self.assertRaisesRegex(ValueError,'digest mismatch'):
                journal.load_plan(self.store,[])

    def test_progress_is_bound_to_plan_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            state={'kind':'chm-epub-retirement','plan_sha256':'a'*64,
                   'inventory_sha256':safety.chm.preview.INVENTORY_SHA256}
            journal.save_progress(self.store,'planned',state,Path(directory))
            self.assertEqual(journal.load_progress(self.store,'a'*64)['phase'],'planned')
            with self.assertRaisesRegex(ValueError,'does not match'):
                journal.load_progress(self.store,'b'*64)

    def test_recovery_refuses_unknown_or_unacknowledged_control(self):
        old='a'*64;new='b'*64
        pointer='reader-index/v3/current.json';promotion=f'reader-index/v3/promotions/{new}.json'
        old_catalog={'current':{'generation':'old'},'generations':{'old':{'generation':'old','status':'current'}}}
        new_catalog={'current':{'generation':'new'},'generations':{
            'old':{'generation':'old','status':'superseded','superseded_at':'2026-10-30T00:00:00Z'},
            'new':{'generation':'new','status':'current'}}}
        catalog_before=publication.encode(old_catalog)
        plan={'new_v3_generation':new,'base_generation':'new','overrides':{
            safety.chm.CATALOG:publication.encode(new_catalog),safety.chm.SIDECAR:b'newside',
            pointer:b'newpointer',promotion:publication.encode({'acks':{'hf':True,'pages':True}}),
            'reader-index/chm-epub-withdrawal-retention.json':b'retention',
            'reader-index/v3/retirement.json':b'newjournal'},'immutable':{'replacement':b'snapshot'}}
        raw_before={safety.chm.CATALOG:catalog_before,safety.chm.SIDECAR:b'oldside',pointer:b'oldpointer',
                    promotion:None,'reader-index/chm-epub-withdrawal-retention.json':None,
                    'reader-index/v3/retirement.json':b'oldjournal','replacement':None}
        before={p:{'present':raw is not None,'sha256':safety.chm.digest(raw) if raw is not None else None,
                   'bytes':len(raw) if raw is not None else None} for p,raw in raw_before.items()}
        before[safety.chm.CATALOG]['raw']=catalog_before.decode()
        self.objects.update({p:r for p,r in raw_before.items() if r is not None})
        self.objects['processing']=b'old'
        updates={'processing':{'before_sha256':safety.chm.digest(b'old'),'after':b'new'}}
        state={'both_surfaces_accepted':False}
        self.assertTrue(journal.classify_recovery_controls(self.store,plan,before,updates,state)['state_only'])
        self.objects['reader-index/v3/retirement.json']=b'newjournal'
        with self.assertRaisesRegex(ValueError,'before recorded two-site'):
            journal.classify_recovery_controls(self.store,plan,before,updates,state)
        self.objects['reader-index/v3/retirement.json']=b'foreign'
        with self.assertRaisesRegex(ValueError,'foreign CHM control'):
            journal.classify_recovery_controls(self.store,plan,before,updates,state)


if __name__=='__main__':
    unittest.main()
