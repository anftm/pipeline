import json
import unittest
from datetime import datetime, timedelta, timezone

from scripts import reader_gc_retirement as gc
from scripts.reader_web_retention_plan import accepted_publication_clock, historical_sidecar_preview, plan, rollback_age
from scripts.reader_web_profile_readback import retention_evidence


def path(hex_digit):
    return 'documents/web/html/' + hex_digit * 64 + '/document.html'


def manifest(p):
    return p.removesuffix('.html') + '-manifest.json'


def refs(*keys):
    return [{'bucket':gc.ASSETS,'path':k} for k in keys]


def root(*members):
    return gc.encode({'version':1,'kind':'reader-web-retained-objects','resources':refs(*members)})


def row(digit):
    p=path(digit)
    return {'bucket':gc.ASSETS,'object':p,'manifest':manifest(p),'source_sha256':'e'*64}


class WebRetentionPlanTests(unittest.TestCase):
    def setUp(self):
        self.a,self.b,self.c=map(path,'abc')
        self.rows=[{'old':row('a'),'new':row('b')}]
        self.root='reader-index/retained-web-batch-'+'d'*64+'.json'
        self.roots={self.root:root(manifest(self.a),manifest(self.b),manifest(self.c))}
        self.hints=dict([gc.record(refs(self.a),[],'1'*32,'catalog-replacement')])

    def test_plan_removes_only_the_one_reviewed_reference(self):
        result=plan(self.rows,self.roots,self.hints)
        self.assertEqual(result['retiring'],sorted([self.a,manifest(self.a)]))
        self.assertEqual(result['roots'][0]['retain'],refs(manifest(self.b),manifest(self.c)))
        self.assertEqual(result['roots'][0]['remove'],[manifest(self.a)])
        self.assertFalse(result['release_authorized'])
        self.assertEqual(result['objects_deleted'],0)

    def test_missing_extra_or_duplicate_reference_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError,'missing independently observed'):
            plan(self.rows,self.roots,{})
        with self.assertRaisesRegex(ValueError,'outside exact old web'):
            plan(self.rows,self.roots,dict([gc.record(refs(self.a,self.b),[],'1'*32,'catalog-replacement')]))
        with self.assertRaisesRegex(ValueError,'do not account'):
            plan(self.rows,{self.root:root(manifest(self.b))},self.hints)
        with self.assertRaisesRegex(ValueError,'duplicate old'):
            plan(self.rows,{self.root:root(manifest(self.a),manifest(self.a))},self.hints)

    def test_web_rollback_is_eligible_at_24_hours_from_verified_replacement(self):
        g='f'*64; t=datetime(2026,11,22,8,tzinfo=timezone.utc)
        proof={'generation':g,'both_projections_verified':True,'old_and_new_verified':1,'verified_at':t.isoformat()}
        before=rollback_age(proof,g,1,t+timedelta(hours=24,microseconds=-1))
        at=rollback_age(proof,g,1,t+timedelta(hours=24))
        self.assertFalse(before['rollback_age_satisfied'])
        self.assertTrue(at['rollback_age_satisfied'])
        self.assertFalse(at['gc_observation_started'])
        for bad in ({'generation':'e'*64},{'both_projections_verified':False},{'old_and_new_verified':True}):
            with self.assertRaisesRegex(ValueError,'dual-site'):
                rollback_age(proof|bad,g,1,t)
        with self.assertRaisesRegex(ValueError,'verification time'):
            rollback_age(proof,g,1,t-timedelta(seconds=1))

    def test_clock_evidence_is_bound_to_completed_publication_and_both_consumers(self):
        g='f'*64; t=datetime(2026,11,22,8,tzinfo=timezone.utc)
        published=gc.encode({'generation':g,'changed_rows':1,'profile':{'rows':self.rows,'converted':1}})
        consumers={'generation':g,'complete':True,'consumers':{
            surface:{'generation':g,'surface':surface,'projection_verified':True,
                     'checked_at':(t-timedelta(seconds=30)).isoformat()} for surface in ('hf','pages')}}
        result={'generation':g,'old_and_new_verified':1,'both_projections_verified':True}
        proof=retention_evidence(result,publication_raw=published,consumer_raw=gc.encode(consumers),now=t)
        self.assertEqual(proof['verified_at'],t.isoformat())
        self.assertEqual(len(proof['publication_sha256']),64)
        report=result|{'web_rollback_verification':proof}
        raw_consumers=gc.encode(consumers)
        self.assertTrue(accepted_publication_clock(published,raw_consumers,report,t+timedelta(hours=24))['rollback_age_satisfied'])
        with self.assertRaisesRegex(ValueError,'differs from accepted artifacts'):
            accepted_publication_clock(published+b' ',raw_consumers,report,t+timedelta(hours=24))
        consumers['consumers']['pages']['generation']='e'*64
        with self.assertRaisesRegex(ValueError,'both verified projections'):
            retention_evidence(result,publication_raw=published,consumer_raw=gc.encode(consumers),now=t)
        with self.assertRaisesRegex(ValueError,'completed publication'):
            retention_evidence(result|{'old_and_new_verified':0},publication_raw=published,
                               consumer_raw=gc.encode(consumers),now=t)

    def test_history_preview_preserves_shared_snapshot_and_out_of_scope_routes(self):
        key='repo\0a.html'
        row=self.rows[0]|{'key':key}
        old={'b':gc.ASSETS,'p':self.a,'m':'h','s':2}
        new=old|{'p':self.b}
        retained={'repo\0b.htm':old|{'p':self.c},'repo\0book.pdf':{'p':'objects/00/pdf'}}
        base={'v':1,'f':{key:old,**retained},'extra':{'unchanged':True}}
        current={'v':1,'f':{key:new,**retained}}
        preview, count=historical_sidecar_preview(base,current,[row])
        self.assertEqual((preview,count),(base|{'f':current['f']},1))
        self.assertEqual(base['f'][key],old)
        self.assertEqual(historical_sidecar_preview(current,current,[row]),(current,0))
        with self.assertRaisesRegex(ValueError,'still present in the accepted successor'):
            historical_sidecar_preview(base,base,[row])
        changed={'v':1,'f':{**current['f'],key:new|{'s':3}}}
        with self.assertRaisesRegex(ValueError,'source or route metadata'):
            historical_sidecar_preview(base,changed,[row])
        shared=base|{'f':{**base['f'],'repo\0alias.html':old}}
        with self.assertRaisesRegex(ValueError,'another route'):
            historical_sidecar_preview(shared,current,[row])

    def test_tampering_does_not_admit_broad_root_or_member(self):
        self.rows[0]['old']['manifest']='some/path'
        with self.assertRaisesRegex(ValueError,'bundle member'):
            plan(self.rows,self.roots,self.hints)
        self.rows[0]['old']['manifest']=manifest(self.a)
        with self.assertRaisesRegex(ValueError,'unreviewed web retention root'):
            plan(self.rows,{'reader-index/catalog.json':self.roots[self.root]},self.hints)
        update=json.loads(self.roots[self.root]);update['extra']='hold'
        with self.assertRaisesRegex(ValueError,'unreviewed web retention root'):
            plan(self.rows,{self.root:gc.encode(update)},self.hints)
