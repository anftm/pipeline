from hashlib import sha256
import gzip
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from scripts import reader_lifecycle
from scripts import reader_web_exact_apply as apply
from scripts import reader_web_exact_bundle as bundle
from scripts import reader_web_exact_legacy as legacy
from scripts import reader_web_exact_stage as stage
from scripts import reader_web_exact_live as live

B = legacy.BUCKET
KEY = 'VoiceOfML/Test\0example.mht'
NOW = '2026-11-22T00:00:00+00:00'


def digest(raw):return sha256(raw).hexdigest()


def row(html):
    source=digest(b'original'); h=digest(html); root='documents/web/mht/'+digest((source+'\0'+h).encode())
    return {'key':KEY,'repo':'VoiceOfML/Test','path':'example.mht','extension':'mht','source_revision':'a'*40,
            'source_sha256':source,'mode':'document-stream','bucket':B,'object':root+'/document.html',
            'manifest':root+'/document-manifest.json','bytes':len(html),'sha256':h}


def packed(routes):return gzip.compress(legacy.publication.encode({'v':1,'f':routes}),compresslevel=9,mtime=0)


class ExactWebTests(unittest.TestCase):
    def setUp(self):
        self.old_html=b'<p>old</p>'; self.new_html='<meta charset="utf-8"><p>中文</p>'.encode()
        self.old=row(self.old_html); self.new=row(self.new_html)
        self.old_manifest=b'{"old":true}'
        self.manifest=legacy.publication.encode({'kind':'web-document-stream','version':1,'path':'document.html',
                        'source_extension':'mht','source_sha256':self.new['source_sha256'],
                        'bytes':len(self.new_html),'sha256':digest(self.new_html)})
        self.other={'s':2,'m':'h','p':'documents/web/html/'+'a'*64+'/document.html','b':B}
        self.routes={KEY:legacy.route(self.old),'Other\0other.html':self.other}
        self.parent=packed(self.routes)
        self.target=legacy.pack(self.parent,self.old,self.new)

    def test_exact_sidecar_delta_and_streamed_catalog_history(self):
        sidecar={'v':1,'f':self.routes}
        before,_=reader_lifecycle.catalog_generation(sidecar,None)
        f=io.BytesIO(legacy.publication.encode(before)); out=io.BytesIO()
        plan=legacy.plan(f,out,before_packed=self.parent,after_packed=self.target,old=self.old,candidate=self.new,
                         now=NOW,parent_sha=digest(f.getvalue()))
        successor=json.loads(out.getvalue())
        self.assertEqual(successor['generations'][plan['parent_generation']]['status'],'superseded')
        refs=successor['generations'][plan['generation']]['references']
        self.assertEqual({ref['path'] for ref in refs},{self.new['object'],self.other['p']})
        self.assertFalse(plan['deletion_authorized'])
        wrong=packed({KEY:legacy.route(self.new),'Other\0other.html':legacy.route(self.new)})
        with self.assertRaisesRegex(ValueError,'exact one-route'):
            legacy.verified_delta(self.parent,wrong,self.old,self.new)

    def stage_args(self, data, writes):
        def read(bucket,path):
            self.assertEqual(bucket,B)
            if path not in data:raise FileNotFoundError(path)
            return data[path]
        def put(path,raw,mime):
            writes.append((path,mime)); data[path]=raw
        snapshot={'path':'reader-index/generations/'+'a'*32+'/reader_assets.json.gz','bytes':len(self.target),
                  'sha256':digest(self.target),'bucket':B}
        return dict(old=self.old,candidate=self.new,html=self.new_html,manifest=self.manifest,
                    old_manifest_sha=digest(self.old_manifest),new_manifest_sha=digest(self.manifest),snapshot=snapshot,
                    snapshot_bytes=self.target,s3_read=read,resolver_read=read,put_once=put,guard=lambda _:True)

    def test_old_retention_is_first_and_recovery_only_observes_exact_bytes(self):
        data={self.old['object']:self.old_html,self.old['manifest']:self.old_manifest}; writes=[]
        args=self.stage_args(data,writes)
        proof=stage.stage(**args)
        self.assertEqual(writes[0][0],proof['old_retention'])
        self.assertEqual(writes[1],(self.new['object'],'text/html; charset=utf-8'))
        self.assertEqual(json.loads(data[proof['old_retention']])['resources'],[{'bucket':B,'path':self.old['manifest']}])
        writes.clear()
        self.assertEqual(proof,stage.stage(**args))
        self.assertEqual(writes,[])
        data[self.old['object']]=b'changed'
        with self.assertRaisesRegex(ValueError,'old objects'):
            stage.stage(**args)
        self.assertEqual(writes,[])

    def test_staging_cannot_reuse_conflicting_content_or_false_window(self):
        data={self.old['object']:self.old_html,self.old['manifest']:self.old_manifest}; writes=[]
        args=self.stage_args(data,writes)
        with self.assertRaisesRegex(RuntimeError,'window'):
            stage.stage(**{**args,'guard':lambda _:False})
        self.assertEqual(writes,[])
        data[stage.retention(self.old)[0]]=b'invalid'
        with self.assertRaisesRegex(ValueError,'staging conflict'):
            stage.stage(**args)
        self.assertEqual(writes,[])

    def test_central_window_cannot_be_inferred_from_actions(self):
        with patch.object(apply.v3,'assert_writer') as check:
            with self.assertRaisesRegex(RuntimeError,'independent'):
                apply.guard_window(None,False)
            check.assert_not_called()

    def test_bundle_rejects_unpinned_hash_or_wrong_member(self):
        raw=io.BytesIO()
        with ZipFile(raw,'w') as z:
            for name in bundle.MEMBERS:z.writestr(name,'x')
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError,'identity'):
                bundle.unpack(raw.getvalue(),'f'*64,Path(temp))
            bundle.unpack(raw.getvalue(),digest(raw.getvalue()),Path(temp))
            self.assertEqual({p.name for p in Path(temp).iterdir()},bundle.MEMBERS)
        bad=io.BytesIO()
        with ZipFile(bad,'w') as z:z.writestr('../outside','x')
        with TemporaryDirectory() as temp,self.assertRaisesRegex(ValueError,'members'):
            bundle.unpack(bad.getvalue(),digest(bad.getvalue()),Path(temp))

    def test_pointer_conditional_adapter_stops_on_byte_drift_or_other_path(self):
        primary=Mock(); primary.read_bytes.return_value=b'old'
        s3=Mock(); s3.read_bytes.return_value=b'different'
        s3.read_etags={(B,apply.v3.POINTER):'"a"'}
        with self.assertRaisesRegex(ValueError,'Hub pointers'):
            apply._check_pointer_phase(primary,s3,b'old')
        adapter=apply.PointerStore(primary,s3,lambda:True,b'old')
        with self.assertRaisesRegex(ValueError,'unrelated'):
            adapter.put_bytes(B,'somewhere/else',b'x')
        with self.assertRaisesRegex(ValueError,'parents differ'):
            adapter.put_if_match(B,apply.v3.POINTER,b'new','"a"')
        s3._client.assert_not_called()

    def test_workflow_is_exact_first_attempt_no_generic_ack_or_dispatch(self):
        from pathlib import Path
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/reader-v3-publish.yml').read_text()
        self.assertIn('group: reader-sidecar',text)
        self.assertIn('group: daily-sync',text)
        body=text.split('if [[ "$OPERATION" == replace-exact-web ]]; then',1)[1].split('if [[ -z "$RESOURCE_JSON"',1)[0]
        self.assertIn('"$GITHUB_RUN_ATTEMPT" == 1',body)
        self.assertLess(body.index('reader_pages_release --preflight'),body.index('reader_web_exact_bundle'))
        self.assertLess(body.index('READER_WEB_PAGES_DISPATCH_SCOPE_REVIEWED'),body.index('reader_web_exact_bundle'))
        readback=text.split('name: Publish and verify exact web consumer projections',1)[1].split('name: Publish the pinned projection to Pages',1)[0]
        self.assertIn('reader_pages_release --publication',readback)
        self.assertIn('reader_web_exact_live',readback)
        self.assertNotIn('ack-all',readback)
        self.assertNotIn('publish_search_reader_index',readback)

    def test_live_check_needs_both_projections(self):
        with self.assertRaisesRegex(ValueError,'HF and Pages'):
            live.verify({'generation':'g'},{'complete':True,'generation':'g','consumers':{'hf':{}}},None)
