import json
import unittest

from scripts import reader_gc_retirement as gc
from scripts.reader_web_retention_plan import plan


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
