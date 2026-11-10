"""EPUB v3 preview changes only the selected compact routes and never writes."""
import copy
import hashlib
import unittest
from unittest.mock import Mock,patch

from scripts import reader_epub_v3_preview as subject,reader_epub_canonical_delivery as delivery
from scripts import reader_epub_canonical_transaction as tx,reader_epub_stage_journal as stage,search_snapshot
from tests import test_reader_epub_canonical_delivery as canonical_fixture


class V3PreviewTests(unittest.TestCase):
    def setUp(self):
        f=canonical_fixture.CanonicalDeliveryTests(methodName='test_one_canonical_write_and_exact_observation');f.setUp();self.addCleanup(f.doCleanups)
        current=f.prepare();self.canonical=f.apply(current);self.f=f
        self.key=f.old['key'];self.old={'s':2,'m':'e','p':f.old['manifest'],'b':subject.backlog.READER_BUCKET,
                                       'c':f.old['manifest'],'cb':subject.backlog.READER_BUCKET}
        self.other_key='VoiceOfML/Test\0another.txt';self.other={'s':2,'m':'t','p':'documents/text/a.txt','b':subject.backlog.READER_BUCKET}
        old='derived/epubtest/'+'a'*32+'/document.pdf';new='objects/bb/'+'b'*64+'/'+'c'*16+'/document.pdf'
        self.pdf_key='VoiceOfML/Test\0paper.pdf'
        self.pdf={'s':2,'m':'p','p':new,'b':subject.redirects.BUCKET}
        self.base={'v':1,'f':{self.key:self.old,self.other_key:self.other,self.pdf_key:self.pdf}}
        self.redirects={old:new};self.pointer={'generation':f.plan['reader_generation']}
        import gzip
        packed=gzip.compress(subject.publication.encode(self.base),mtime=0);digest=hashlib.sha256(packed).hexdigest()
        ref=subject.publication.metadata(subject.backlog.READER_BUCKET,f'reader-index/snapshots/{digest}/reader_assets.json.gz',packed)
        self.catalog={'version':3,'kind':'reader-reading-catalog','files':{},'base_sidecar':ref,
                      'derived_pdf_redirects':self.redirects,'suppressed_sources':[],'generation':f.plan['reader_generation']}
        self.store=Mock();self.client=Mock();self.validator=f.validator
        self.patches=[patch.object(subject.publication,'current',return_value=(self.pointer,self.catalog)),
            patch.object(subject.publication,'pinned_base',return_value=self.base),
            patch.object(subject.publication,'validate_catalog',side_effect=lambda x:x),
            patch.object(subject.publication,'observe_consumer',return_value={'generation':self.pointer['generation'],'projection_verified':True})]
        for item in self.patches:item.start();self.addCleanup(item.stop)
    def inspect(self):
        return subject.inspect(self.store,self.f.store,self.f.index_store,self.f.target,self.f.plan['operation'],
            checkpoints=self.f.checkpoints,protections=self.f.protections,objects=self.f.objects,
            revalidate=self.validator,client=self.client)
    def test_selected_update_keeps_other_routes_and_pdf_redirects(self):
        original=copy.deepcopy(self.base);result,packed=self.inspect()
        self.assertEqual(self.base,original);self.assertEqual(result['selected_keys'],[self.key])
        self.assertEqual(result['previous_routes'][self.key],self.old)
        self.assertNotEqual(result['candidate_routes'][self.key]['p'],self.old['p'])
        self.assertEqual(subject._decode(packed)['f'][self.other_key],self.other)
        self.assertEqual(subject._decode(packed)['f'][self.pdf_key],self.pdf)
        self.assertEqual(result['candidate_catalog']['derived_pdf_redirects'],self.redirects)
        self.assertEqual(result['preserved_pdf_redirects'],1);self.assertEqual(result['remote_writes'],0)
        self.assertFalse(result['pointer_written']);self.assertFalse(result['publication_ready'])
        self.store.put_bytes.assert_not_called()
    def test_changed_old_route_override_or_suppression_stops(self):
        self.base['f'][self.key]['p']='foreign'
        with self.assertRaisesRegex(ValueError,'old EPUB route differs'):self.inspect()
        self.base['f'][self.key]=copy.deepcopy(self.old)
        self.catalog['files'][self.key]={'resource':{}}
        with self.assertRaisesRegex(ValueError,'suppression or v3 override'):self.inspect()
        self.catalog['files'].clear();self.catalog['suppressed_sources']=[self.key]
        with self.assertRaises(ValueError):self.inspect()
    def test_unapplied_pdf_redirect_never_becomes_a_new_base(self):
        old=next(iter(self.redirects));self.base['f'][self.pdf_key]['p']=old
        with self.assertRaisesRegex(ValueError,'does not preserve current PDF redirects'):self.inspect()
        self.store.put_bytes.assert_not_called()

    def test_missing_bundle_or_stale_consumer_blocks(self):
        action=self.f.preview['actions'][0];self.f.protections.values.pop(action['protection_path'])
        with self.assertRaisesRegex(ValueError,'protection changed'):self.inspect()
        with patch.object(subject.publication,'observe_consumer',return_value={'generation':'f'*64,'projection_verified':True}):
            # Restore exact protection before reaching consumer checks.
            from scripts import reader_epub_protection
            self.f.protections.values[action['protection_path']]=search_snapshot.canonical(reader_epub_protection.make_record(
                self.f.plan['operation'],self.f.plan['plan_sha256'],action['root'],stage.load(self.f.store,self.f.plan['operation']).intent['created_at']))
            with self.assertRaisesRegex(ValueError,'consumer changed'):self.inspect()
    def test_changed_checkpoint_validator_is_rejected(self):
        value=self.f.checkpoints.values[tx.CANONICAL][0];self.f.checkpoints.values[tx.CANONICAL]=(value,'"other"')
        with self.assertRaisesRegex(ValueError,'validator changed'):self.inspect()


if __name__=='__main__':unittest.main()
