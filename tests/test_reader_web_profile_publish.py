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

    def test_200_payload_pairs_keep_per_object_checks_without_400_full_inventories(self):
        full, per_object = Mock(return_value=True), Mock(return_value=True)
        clock = Mock(return_value=0)
        guard = web._immutable_payload_guard(full, per_object, clock=clock)
        for second in range(400):
            clock.return_value=second
            self.assertIs(guard(), True)
        self.assertEqual(full.call_count, 14)
        self.assertEqual(per_object.call_count, 386)

    def test_payload_guard_does_not_cache_a_failed_boundary_or_ignore_live_state(self):
        full = Mock(side_effect=[False, True, RuntimeError('competing writer')])
        local = Mock(side_effect=RuntimeError('Search or maintenance moved'))
        guard = web._immutable_payload_guard(full, local, clock=lambda: 0)
        self.assertIs(guard(), False)
        self.assertIs(guard(), True)
        with self.assertRaisesRegex(RuntimeError, 'maintenance moved'):
            guard()
        with self.assertRaisesRegex(RuntimeError, 'competing writer'):
            guard()

    def test_slow_payload_read_refreshes_expired_full_inventory(self):
        clock = Mock(return_value=0)
        full = Mock(return_value=True)
        def slow():
            clock.return_value=40
            return True
        guard = web._immutable_payload_guard(full, slow, clock=clock)
        self.assertTrue(guard())
        clock.return_value=25
        self.assertTrue(guard())
        self.assertEqual(full.call_count, 2)

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

    def test_two_hundred_outcomes_consume_slots_before_any_metadata_checkpoint(self):
        ext, search, generation = 'html', 'b'*64, 'd'*64
        old = {f'book-{i:03d}': {'key': f'book-{i:03d}', 'repo': 'org/books', 'path': f'book-{i:03d}.html',
                               'object': f'old/{i}/document.html', 'manifest': f'old/{i}/document-manifest.json',
                               'source_sha256': 'a'*64, 'bytes': 2, 'sha256': 'e'*64} for i in range(204)}
        old['book-193']['bytes'] = web.MAX_PUBLIC_WEB_BYTES + 1
        entries = {k: {'state': 'profile_outdated', 'recorded_failure': False,
                       **{f'previous_{field}': row[field] for field in ('source_sha256','object','manifest','sha256','bytes')}}
                   for k, row in old.items()}
        records = {k: {'extension': ext, 'revision': 'f'*40} for k in old}
        hub = Mock(); hub.read_bytes.return_value = b'parent'
        cps = Mock(); cps.read.return_value = SimpleNamespace(raw=b'parent')

        def inspect(**kwargs):
            kwargs['details_out'].update(rows=entries, records=records)
            return {'search_generation': search, 'reader_generation': generation}, None

        def attempt(item, before, *, reviewed):
            if int(before['key'][-3:]) >= 194:
                raise web.convert.Hold('parity_anchors')
            return before, b'ok', b'manifest', {}

        with patch.object(web.v3, 'CentralHubStore', return_value=hub), patch.object(web.daily, 'GitHub'), \
                patch.object(web, 'ordinary_writer', return_value=True), patch.object(web, '_owner'), \
                patch.object(web.v3, 'current', return_value=({'generation': generation},)), \
                patch.object(web.pages, 'current', return_value={'search_generation': search}), \
                patch.object(web, 'ConditionalCheckpointStore', return_value=cps), patch.object(web, 'LegacyRoots'), \
                patch.object(web.index, '_index', return_value=(None, old)), patch.object(web.backlog, 'inspect', side_effect=inspect), \
                patch.object(web.daily, 'validate_summary', return_value={ext: {}}), patch.object(web.daily, 'blockers', return_value=[]), \
                patch.object(web, 'candidate', side_effect=attempt) as convert, patch.object(web, 'checkpoint_non_routing') as checkpoint, \
                patch.dict('os.environ', {'GH_TOKEN': 'test', 'READER_WEB_PROFILE_CANDIDATE_INDEX': ''}):
            publication = web.execute({'extension': ext, 'search_generation': search, 'parent_run_id': 23}, None)
        self.assertLessEqual(web.daily.WEB_PROFILE_BATCH_ROWS, web.prepare.plan.MAX_MANAGED_ROWS)
        self.assertEqual(convert.call_count, 199)
        self.assertEqual((publication['profile']['converted'], publication['profile']['identical'],
                          publication['profile']['held']), (0, 193, 7))
        self.assertEqual(len(checkpoint.call_args.args[4]), 193)
        self.assertEqual(set(checkpoint.call_args.args[5]), {f'book-{i:03d}' for i in range(193, 200)})
        self.assertEqual(publication['profile']['holds']['book-193']['error'], 'old_web_readback_size')

    def test_shared_manifest_is_deferred_before_replay_not_marked_held(self):
        ext, search, generation = 'html', 'b'*64, 'd'*64
        old = {key: {'key': key, 'object': 'old/shared/document.html',
                     'manifest': 'old/shared/document-manifest.json', 'source_sha256': 'a'*64,
                     'bytes': 2, 'sha256': 'e'*64} for key in ('a', 'b')}
        entries = {k: {'state': 'profile_outdated', 'recorded_failure': False,
                       **{f'previous_{field}': row[field] for field in ('source_sha256','object','manifest','sha256','bytes')}}
                   for k, row in old.items()}
        records = {k: {'extension': ext} for k in old}
        hub = Mock(); hub.read_bytes.return_value = b'parent'
        cps = Mock(); cps.read.return_value = SimpleNamespace(raw=b'parent')

        def inspect(**kwargs):
            kwargs['details_out'].update(rows=entries, records=records)
            return {'search_generation': search, 'reader_generation': generation}, None

        def attempt(item, before, *, reviewed):
            key = before['key']
            return before | {'object': f'new/{key}/document.html', 'manifest': f'new/{key}/document-manifest.json',
                             'sha256': key*64}, key.encode(), b'manifest', {}

        with patch.object(web.v3, 'CentralHubStore', return_value=hub), patch.object(web.daily, 'GitHub'), \
                patch.object(web, 'ordinary_writer', return_value=True), patch.object(web, '_owner'), \
                patch.object(web.v3, 'current', return_value=({'generation': generation},)), \
                patch.object(web.pages, 'current', return_value={'search_generation': search, 'code_sha': 'f'*40}), \
                patch.object(web, 'ConditionalCheckpointStore', return_value=cps), patch.object(web, 'LegacyRoots'), \
                patch.object(web.index, '_index', return_value=(None, old)), patch.object(web.backlog, 'inspect', side_effect=inspect), \
                patch.object(web.daily, 'validate_summary', return_value={ext: {}}), patch.object(web.daily, 'blockers', return_value=[]), \
                patch.object(web, 'candidate', side_effect=attempt), \
                patch.object(web.replay, 'verify', side_effect=RuntimeError('stop before writes')) as verify, \
                patch.object(web, 'checkpoint_non_routing') as checkpoint, \
                patch.dict('os.environ', {'GH_TOKEN': 'test', 'READER_WEB_PROFILE_CANDIDATE_INDEX': ''}):
            with self.assertRaisesRegex(RuntimeError, 'stop before writes'):
                web.execute({'extension': ext, 'search_generation': search, 'parent_run_id': 23}, None)
        verify.assert_called_once_with({'a'*64: b'a'}, 'f'*40)
        checkpoint.assert_not_called()

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
