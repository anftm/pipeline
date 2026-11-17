import copy
import json
import unittest
from scripts import bha_snapshot_gc as gc,search_snapshot

NOW=1800000000
CURRENT='bha-1799999990-aaaaaaaa'
PREVIOUS='bha-1795000000-bbbbbbbb'
OLD='bha-1790000000-cccccccc'

class Store:
    def __init__(self):self.objects={};self.values={};self.deletes=[];self.unknown=False;self.after_delete=None
    def add(self,key,raw=b'fixture',modified=1700000000):
        self.values[key]=raw;self.objects[key]={'bytes':len(raw),'etag':'"'+gc.sha(raw)+'"','modified':modified}
    def inventory(self):return copy.deepcopy(self.objects)
    def read(self,key):return self.values[key]
    def create(self,key,value):
        if key in self.objects:raise ValueError('exists')
        self.add(key,search_snapshot.canonical(value),NOW)
    def delete(self,key,etag):
        assert self.objects[key]['etag']==etag
        self.deletes.append(key);del self.objects[key];del self.values[key]
        if self.after_delete:self.after_delete()
        if self.unknown:raise TimeoutError('lost response')

class SnapshotGcTests(unittest.TestCase):
    def setUp(self):
        self.s=Store();self.status={'index_status':'ready','switch_pending':False,'job':None,
            'archive_revisions':dict.fromkeys([str(i) for i in range(32)],'a'*40),'build_fingerprint':'b'*64}
        self.manifest={'schema':3,'generation':CURRENT,'index_metadata':{'archive_revisions':self.status['archive_revisions'],'build_fingerprint':'b'*64},'sidecars':{'docs':{'filename':'docs.sqlite','bytes':7},'facets':{'filename':'facets.sqlite','bytes':7}}}
        self.s.add('current.json',json.dumps(self.manifest).encode())
        for g in [CURRENT,PREVIOUS,OLD]:
            for name in ['docs.sqlite','facets.sqlite','elasticsearch/index-0']:self.s.add('generations/'+g+'/'+name)
    def run_gc(self,now=NOW,apply=True):return gc.run(self.s,'fixture',apply=apply,status=lambda _:copy.deepcopy(self.status),now=now)
    def test_mark_delay_and_bounded_delete_preserve_current_previous(self):
        self.assertEqual(self.run_gc()['status'],'marked');self.assertFalse(self.s.deletes)
        self.assertEqual(self.run_gc()['status'],'mark-waiting')
        r=self.run_gc(NOW+gc.MARK_DELAY+1)
        self.assertEqual(r['status'],'deleted');self.assertEqual(r['deleted_objects'],3)
        self.assertTrue(all(OLD in p for p in self.s.deletes))
        self.assertEqual(self.run_gc(NOW+gc.MARK_DELAY+2)['status'],'no-candidates')
    def test_current_only_never_deleted(self):
        for key in list(self.s.objects):
            if PREVIOUS in key or OLD in key:del self.s.objects[key];del self.s.values[key]
        self.assertEqual(self.run_gc()['status'],'no-candidates');self.assertFalse(self.s.deletes)
    def test_journal_or_other_control_pins_old_generation(self):
        self.s.add('bha-upstream-auto/objects/receipt.json',json.dumps({'snapshot_generation':OLD}).encode())
        self.assertEqual(self.run_gc()['status'],'no-candidates')
    def test_recent_object_keeps_whole_generation(self):
        self.s.objects['generations/'+OLD+'/docs.sqlite']['modified']=NOW
        self.assertEqual(self.run_gc()['status'],'no-candidates')
    def test_unknown_delete_never_replayed(self):
        self.run_gc();self.s.unknown=True
        with self.assertRaises(TimeoutError):self.run_gc(NOW+gc.MARK_DELAY+1)
        count=len(self.s.deletes)
        with self.assertRaises(ValueError):self.run_gc(NOW+gc.MARK_DELAY+2)
        self.assertEqual(len(self.s.deletes),count)
    def test_new_control_during_sweep_stops_remaining_deletion(self):
        self.run_gc();self.s.after_delete=lambda:self.s.add('new-pin.json',json.dumps({'generation':OLD}).encode())
        with self.assertRaisesRegex(ValueError,'control root'):self.run_gc(NOW+gc.MARK_DELAY+1)
        self.assertEqual(len(self.s.deletes),1)
    def test_changed_identity_active_index_and_unknown_root_block(self):
        self.run_gc();self.s.objects['generations/'+OLD+'/docs.sqlite']['etag']='"changed"'
        with self.assertRaises(ValueError):self.run_gc(NOW+gc.MARK_DELAY+1)
        self.status['job']={'state':'running'}
        with self.assertRaises(ValueError):self.run_gc()
        self.status['job']=None;self.s.add('unknown-file')
        with self.assertRaises(ValueError):self.run_gc()
