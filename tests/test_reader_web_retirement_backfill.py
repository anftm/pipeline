import unittest
from unittest.mock import Mock, patch

from scripts import reader_gc_retirement as gc, reader_web_retirement_backfill as backfill


def bundle(h):
    root='documents/web/html/'+h*64
    return {'bucket':gc.ASSETS,'object':root+'/document.html','manifest':root+'/document-manifest.json','sha256':h*64,'bytes':10}


class WebRetirementBackfillTests(unittest.TestCase):
    def setUp(self):
        self.row={'key':'VoiceOfML/Library\0book.html','old':bundle('a'),'new':bundle('b'),
                  'old_manifest_sha256':'c'*64,'new_manifest_sha256':'d'*64}
        self.g='e'*64
        self.hint=list(gc.record([{'bucket':gc.ASSETS,'path':self.row['old']['object']}],[],'f'*32,'catalog-replacement'))
        self.data={'rows':[self.row],'generation':self.g,'hints':[self.hint]}

    def test_scope_rejects_reclaimed_targets_or_missing_hints(self):
        self.assertEqual(backfill.parse(self.data),self.data)
        with self.assertRaises(ValueError):
            backfill.parse(self.data|{'hints':[]})
        with self.assertRaises(ValueError):
            backfill.parse(self.data|{'rows':[self.row|{'new':self.row['old']}]})
        with self.assertRaises(ValueError):
            backfill.parse(self.data|{'hints':[self.hint[:1]+[self.hint[1]|{'reason':'v3-replacement'}]]})

    def test_verified_projection_and_assets_before_hint(self):
        expected={self.row['key']:{'b':gc.ASSETS,'m':'h','p':self.row['new']['object']}}
        with patch.object(backfill.v3,'current',return_value=({'generation':self.g},{'base_sidecar':True})), \
                patch.object(backfill.v3,'project_sidecar',return_value={'f':expected}), \
                patch.object(backfill.v3,'pinned_base'), patch.object(backfill.v3,'verify_generation') as verify, \
                patch.object(backfill,'live',return_value={'both_projections_verified':True}) as live, \
                patch.object(backfill,'record_hint',return_value=self.hint[0]) as write:
            kwargs={'hub':Mock(),'http':Mock(),'guard':lambda:True}
            preview=backfill.run(self.data,**kwargs)
            self.assertFalse(preview['pin_release_authorized']);write.assert_not_called()
            out=backfill.run(self.data,**kwargs,claims=Mock())
            self.assertEqual(out['hints_written_or_verified'],[self.hint[0]])
            verify.assert_called();live.assert_called();write.assert_called_once()
            expected[self.row['key']]['p']=self.row['old']['object']
            with self.assertRaisesRegex(ValueError,'current projection'):
                backfill.run(self.data,**kwargs,claims=Mock())
            self.assertEqual(write.call_count,1)
