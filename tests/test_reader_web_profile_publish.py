"""Fail-closed exact profile publisher and common lock contracts."""
import json,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from scripts import reader_gc_retirement as retirement, reader_web_profile_publish as web
from scripts.reader_web_profile_readback import record_hint
from scripts.reader_web_profile import WEB_PROFILE


class AutomaticWebProfileTests(unittest.TestCase):
    def test_controller_scope_allows_only_one_web_extension_and_pinned_parent(self):
        p={'extension':'html','search_generation':'a'*64,'parent_run_id':23}
        self.assertEqual(web.authority_scope(p),('html','a'*64,23))
        for k,v in (('extension','pdf'),('search_generation','latest'),('parent_run_id',True)):
            with self.subTest(field=k),self.assertRaises(ValueError):web.authority_scope(p|{k:v})
        with self.assertRaises(ValueError):web.authority_scope(p|{'external_writers_stopped':True})

    def test_writer_checks_maintenance_and_rejects_competing_runs(self):
        owner=('42','1','a'*40); parent={'status':'in_progress','id':10}
        auth=Mock();auth.get_run.side_effect=lambda n:parent if n==10 else {'display_title':'reader-auto/'+'c'*32,'actor':{'login':'github-actions[bot]'}}
        auth.active_children.return_value=[{'id':42}]
        state={'kind':'reader-maintenance-journal','version':1,'restored':True}
        h=Mock(); h.assert_serialized_writer=Mock(); h.read_bytes.return_value=json.dumps(state).encode()
        with patch.object(web,'_owner',return_value=owner),patch.object(web,'_active_owner'), \
                patch.object(web.daily,'validate_daily_run'),patch.dict('os.environ',{'READER_WEB_PROFILE_REBUILD_ENABLED':'true','READER_WEB_PROFILE_PAGES_SCOPE_REVIEWED':'true','WEB_PROFILE_AUTOMATION_ID':'c'*32}):
            self.assertTrue(web.ordinary_writer(h,10,auth,observed=owner))
            auth.active_children.return_value.append({'id':99})
            with self.assertRaisesRegex(RuntimeError,'competing'):web.ordinary_writer(h,10,auth,observed=owner)
            auth.active_children.return_value=[]
            h.read_bytes.return_value=json.dumps(state|{'callback_mutation_started':True}).encode()
            with self.assertRaisesRegex(RuntimeError,'maintenance'):web.ordinary_writer(h,10,auth,observed=owner)

    def test_metadata_holds_and_identical_results_do_not_change_route(self):
        old={'a':{'key':'a','object':'unchanged'},'b':{'key':'b','object':'keep'}}
        value={'version':1,'kind':'web-document-stream-index','files':list(old.values()),'failures':[]}
        raw=web.index._encode(value); h=Mock();h.read_bytes.side_effect=[raw,raw,b'bad']
        cps=Mock();snap=SimpleNamespace(raw=raw,value=value,raw_sha256='e'*64,etag='"old"');cps.read.return_value=snap; journal=Mock();journal.read.return_value=None;journal.claim.return_value=True
        with patch.object(web.index,'S3Journal',return_value=journal),patch.dict('os.environ',{'WEB_PROFILE_AUTOMATION_ID':'c'*32}):
            with self.assertRaisesRegex(RuntimeError,'not observed'):
                web.checkpoint_non_routing(cps,h,'documents/web/html/index.json',old,{'a':old['a']},{'b':{'key':'b','profile':WEB_PROFILE}},lambda:True)
        self.assertEqual(cps.write_once.call_count,1)
        payload=cps.write_once.call_args.args[2]
        self.assertEqual(payload['files'],[old['a']|{'profile':WEB_PROFILE},old['b']])
        self.assertEqual(payload['failures'],[{'key':'b','profile':WEB_PROFILE}])

    def test_replay_requires_every_candidate_on_both_surfaces(self):
        docs={'a':b'a','b':b'b'}
        passes={'a':{'passed':True},'b':{'passed':True}}
        fails={'a':{'passed':True},'b':{'passed':False}}
        self.assertEqual(web.replay.rejected({'pages':{'documents':passes},'space':{'documents':fails}},docs),{'b'})
        for evidence in ({'pages':{'documents':passes}},
                         {'pages':{'documents':passes},'space':{'documents':{'a':{'passed':True}}}}):
            with self.assertRaisesRegex(ValueError,'both sites'):
                web.replay.rejected(evidence,docs)

    def test_retirement_hint_is_idempotent_and_bound_to_old_web_routes(self):
        old = {'bucket':web.B,'object':'documents/web/html/'+'a'*64+'/document.html'}
        new = {'bucket':web.B,'object':'documents/web/html/'+'b'*64+'/document.html'}
        rows = [{'old':old,'new':new}]
        p,v = retirement.record([{'bucket':web.B,'path':old['object']}],[],'a'*32,'catalog-replacement')
        data = {'retirement_hint':[p,v]}; raw = retirement.encode(v)
        hub = Mock(); hub.read_bytes.return_value = raw
        claims = Mock(); claims._get.side_effect = [(None,None),(raw,'etag'),(raw,'etag'),(raw,'etag')]
        self.assertEqual(record_hint(data,rows,guard=lambda:True,hub=hub,claims=claims),p)
        self.assertEqual(record_hint(data,rows,guard=lambda:True,hub=hub,claims=claims),p)
        claims._put.assert_called_once_with(p,raw,IfNoneMatch='*')
        with self.assertRaisesRegex(ValueError,'sealed replacement'):
            record_hint(data,[{'old':new,'new':old}],guard=lambda:True,hub=hub,claims=claims)
        with self.assertRaisesRegex(RuntimeError,'fence lost'):
            record_hint(data,rows,guard=lambda:False,hub=hub,claims=claims)

    def test_automatic_switch_preserves_joint_locks_and_no_legacy_pages_fallback(self):
        w=(Path(__file__).parents[1]/'.github/workflows/reader-v3-publish.yml').read_text()
        self.assertIn('group: reader-sidecar',w);self.assertIn('group: daily-sync',w)
        apply=w.split('if [[ "$OPERATION" == upgrade-web-profile ]]; then',1)[1].split('exit 0',1)[0]
        self.assertIn('READER_WEB_PROFILE_PAGES_SCOPE_REVIEWED',apply)
        self.assertIn('reader_pages_release --preflight',apply)
        self.assertIn('playwright install --with-deps chromium --only-shell',w)
        self.assertIn('output/v3/web-profile-replay.json',w)
        readback=w.split('name: Publish and verify automatic web profile on both sites',1)[1].split('- name:',1)[0]
        self.assertIn('format-automation-verification.json',readback)
        self.assertIn('reader_web_profile_readback',readback)
        self.assertNotIn('publish_search_reader_index',readback)
