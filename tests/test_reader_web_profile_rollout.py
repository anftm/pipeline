import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pilot_web_stream as worker,reader_web_profile_rollout as rollout,reader_web_batch_plan as batch
from scripts.reader_web_profile import WEB_PROFILE
from scripts.reader_web_profile_summary import summarize
from scripts.reader_web_managed_convert import digest
from scripts.publish_reader_v3 import encode


def sample(ext,number,profile=None):
    repo='VoiceOfML/Books';path=f'{number}.{ext}';key=repo+'\0'+path; r='a'*40;s='d'*64; h='e'*64
    root=f'documents/web/{ext}/'+'c'*64
    old={'key':key,'repo':repo,'path':path,'extension':ext,'source_revision':r,'source_sha256':s,
         'mode':'document-stream','bucket':worker.TARGET_BUCKET,'object':root+'/document.html','manifest':root+'/document-manifest.json','bytes':10,'sha256':h}
    if profile:old['profile']=profile
    return {'key':key,'extension':ext,'repo':repo,'path':path,'revision':'f'*40},old


class WebProfileRolloutTests(unittest.TestCase):
    def test_version_backlog_preserves_old_source_pin_and_bounds_shards(self):
        inputs=[sample('html',i) for i in range(81)]+[sample('htm',1,WEB_PROFILE),sample('mht',1),sample('mhtml',1)]
        records={x['key']:x for x,_ in inputs};indexes={e:{'kind':rollout.KIND,'version':1,'files':[o for _,o in inputs if o['extension']==e]} for e in ('html','htm','mht')}
        # Historical canonical entries are not reconverted.
        indexes['html']['files'].append(sample('html',1000)[1]); groups,counts,holds=rollout.plan(records,indexes)
        self.assertEqual(counts,{'current':1,'unmapped':1,'held':0,'queued':82})
        self.assertFalse(holds);self.assertEqual([len(x['items']) for _,x in groups],[40,40,1,1])
        self.assertEqual({p['source_revision'] for _,x in groups for p in x['items']},{'a'*40})

    def test_new_ingest_defaults_to_profile_without_replacing_revision_identity(self):
        args=['pilot_web_stream.py','--extension','html']
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'state.json').write_text('{}')
            args+=['--revisions',str(root/'state.json')]
            def fake(*args,**kwargs):
                build=kwargs['build']; item={'repo':'VoiceOfML/Books','path':'x.html','revision':'a'*40,'extension':'html'}
                with patch.object(worker,'download',side_effect=lambda _,path,__:path.write_bytes('<h1>中文</h1><p>abcd</p>'.encode())):
                    entry,files=build(item,root/'run',None,worker.TARGET_BUCKET)
                self.assertEqual((entry['source_revision'],entry['profile']),('a'*40,WEB_PROFILE))
                self.assertIn('viewport',Path(files[entry['object']]).read_text()); return 0
            with patch('sys.argv',args),patch.object(worker,'select_records',return_value=[]),patch.object(worker,'run_documents',side_effect=fake):
                self.assertEqual(worker.main(),0)

    def test_summary_requires_every_source_result(self):
        src,old=sample('html',1); ix={'html':{'kind':rollout.KIND,'version':1,'files':[old]}}
        groups,counts,holds=rollout.plan({src['key']:src},ix)
        name,request=groups[0]; req=encode(request); run={'profile':WEB_PROFILE,'reader_generation':'r','search_generation':'s','counts':counts,'remote_writes':0}
        with tempfile.TemporaryDirectory() as folder:
            d=Path(folder); (d/'report.json').write_bytes(encode(run)); (d/'matrix.json').write_bytes(encode({'include':[{'shard':name}]}))
            (d/(name+'.json')).write_bytes(req)
            dest=d/f'managed-web-profile-{name}';dest.mkdir(); ledger={'request_sha256':digest(req),'profile':WEB_PROFILE,'remote_writes':0,
                'publication_ready':False,'items':[dict(request['items'][0],status='hold',reason='missing_image')]}
            (dest/'report.json').write_bytes(encode(ledger))
            self.assertEqual(summarize(d,d)['hold_reasons'],{'missing_image':1})
            (dest/'report.json').write_bytes(encode({**ledger,'items':[]}))
            with self.assertRaises(ValueError):summarize(d,d)

    def test_managed_canonical_updates_derivative_version_with_original_untouched(self):
        _,old=sample('html',1); n='f'*64; root='documents/web/html/'+batch.digest((old['source_sha256']+'\0'+n).encode())
        new={**old,'object':root+'/document.html','manifest':root+'/document-manifest.json','sha256':n,'profile':WEB_PROFILE}
        raw=encode({'kind':rollout.KIND,'version':1,'files':[old],'failures':[]}); out=batch.canonical({'html':raw},[(old,new)],'1'*32,managed=True)
        self.assertEqual(json.loads(out['html'])['files'],[new])
