"""The Reader Pages operation cannot accidentally push or replay a dispatch."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import unittest

import yaml
from unittest.mock import MagicMock, Mock, patch

from scripts import reader_pages_release as release

CODE='a'*40
WORKFLOW='b'*40
OLD='c'*64
READER='d'*64
CATALOG='e'*64
SEARCH='f'*64
NOW=datetime(2026,11,20,5,tzinfo=timezone.utc)


class MemoryClaims:
    def __init__(self):
        self.values={}

    def read(self,k):
        return self.values.get(k)

    def create(self,k,v):
        if k in self.values:raise RuntimeError('conditional creation conflict')
        self.values[k]=v


class ReaderPagesReleaseTests(unittest.TestCase):
    def setUp(self):
        self.storage=MemoryClaims()
        self.session=MagicMock()
        result=self.session.post.return_value.__enter__.return_value
        result.status_code=204
        self.target={'reader_generation':READER,'reader_catalog_sha256':CATALOG,
                     'search_generation':SEARCH,'search_head':'1'*64}
        self.live={'version':1,'run_id':42,'run_attempt':1,'code_sha':CODE,'workflow_sha':CODE,
                   'search_generation':SEARCH,'reader_generation':OLD,'reader_catalog_sha256':'2'*64}
        self.verify=Mock(return_value={'complete':True})
        self.clock=Mock(return_value=0)

    def execute(self, **opts):
        return release.release(**dict({'session':self.session,'generation':READER,'write_store':self.storage,
            'read_target':lambda:dict(self.target),'read_live':lambda:dict(self.live),'verify':self.verify,
            'workflow_sha':WORKFLOW,'pipeline_run':'999','clock':self.clock,'sleep':Mock(),'now':NOW,'wait':0},**opts))

    def run_record(self):
        [v]=self.storage.values.values()
        return {'id':43,'display_title':'reader-refresh/'+v['operation'],'created_at':v['created'],
            'head_sha':v['workflow_sha'],'head_branch':'main','event':'workflow_dispatch',
            'path':release.STATIC,'run_attempt':1,'status':'completed','conclusion':'success'}

    def expose_claim(self,*_args,**_kwargs):
        r=self.run_record();[v]=self.storage.values.values()
        self.live.update({'run_id':r['id'],'reader_refresh_id':v['operation'],'workflow_sha':WORKFLOW,
                          'reader_generation':READER,'reader_catalog_sha256':CATALOG})
        return [r]

    def test_request_uses_exact_workflow_code_and_data_and_checks_deployed_receipt(self):
        with patch.object(release,'list_runs',side_effect=self.expose_claim):
            result=self.execute()
        [request]=self.storage.values.values()
        self.session.post.assert_called_once_with(release.API+'/actions/workflows/static.yml/dispatches',
            json={'ref':'main','inputs':{'reader_refresh_id':request['operation'],'reader_code_sha':CODE,
            'reader_workflow_sha':WORKFLOW,'reader_generation':READER,'reader_catalog_sha256':CATALOG,
            'search_generation':SEARCH}},timeout=25,allow_redirects=False)
        self.verify.assert_called_once_with(READER)
        self.assertEqual(result['pages_run'],43)

    def test_verified_noop_does_not_claim_or_dispatch(self):
        self.live.update(reader_generation=READER,reader_catalog_sha256=CATALOG)
        self.assertEqual(self.execute()['status'],'already-deployed')
        self.session.post.assert_not_called()
        self.assertFalse(self.storage.values)

    def test_lost_response_spends_claim_even_when_rediscovering_with_new_code(self):
        self.session.post.side_effect=TimeoutError('response lost')
        with self.assertRaises(TimeoutError):self.execute()
        saved=dict(self.storage.values)
        self.live.update(code_sha='3'*40)
        with patch.object(release,'list_runs',return_value=[]),self.assertRaisesRegex(TimeoutError,'outcome unresolved'):
            self.execute(workflow_sha='4'*40,pipeline_run='1000')
        self.assertEqual(self.storage.values,saved)
        self.session.post.assert_called_once()

    def test_lost_response_reconciles_original_first_attempt(self):
        self.session.post.side_effect=TimeoutError('response lost')
        with self.assertRaises(TimeoutError):self.execute()
        with patch.object(release,'list_runs',side_effect=self.expose_claim):
            self.assertEqual(self.execute(observe_only=True)['status'],'deployed')
        self.session.post.assert_called_once()

    def test_public_receipt_observations_bypass_intermediate_caches(self):
        get = Mock(return_value=json.dumps(self.live).encode())
        self.assertEqual(release.read_live_receipt(get=get), self.live)
        self.assertEqual(release.read_live_receipt(get=get), self.live)
        urls = [call.args[0] for call in get.call_args_list]
        self.assertEqual(len(set(urls)), 2)
        for url in urls:
            self.assertRegex(url, '^' + re.escape(release.RECEIPT) + r'\?reader_check=[a-f0-9]{32}$')

    def test_state_drift_is_refused_during_acceptance(self):
        self.live.update(reader_generation=READER,reader_catalog_sha256=CATALOG)
        read_live=Mock(side_effect=[self.live,self.live | {'run_id':44}])
        with self.assertRaisesRegex(ValueError,'changed during acceptance'):
            self.execute(read_live=read_live)
        self.session.post.assert_not_called()

    def test_state_drift_is_refused_before_claim(self):
        later={**self.live,'run_id':44}
        read_live=Mock(side_effect=[self.live,later])
        with self.assertRaisesRegex(ValueError,'moved before dispatch'):self.execute(read_live=read_live)
        self.session.post.assert_not_called()
        self.assertFalse(self.storage.values)

    def test_reject_rerun_and_duplicate_even_if_live_receipt_matches(self):
        for mode in ('rerun','duplicate'):
            self.storage.values.clear()
            self.live.update(reader_generation=OLD,reader_catalog_sha256='2'*64)
            def invalid(*_a,**_kw):
                rows=self.expose_claim()
                return [{**rows[0],'run_attempt':2}] if mode=='rerun' else rows*2
            with patch.object(release,'list_runs',side_effect=invalid),self.assertRaises(ValueError):self.execute()
        self.verify.assert_not_called()

    def test_successful_actions_run_without_exact_live_receipt_does_not_count(self):
        with patch.object(release,'list_runs',side_effect=lambda *_a,**_k:[self.run_record()]):
            with self.assertRaisesRegex(ValueError,'receipt does not match'):self.execute()
        self.verify.assert_not_called()

    def test_preflight_refuses_admin_maintainer_or_unproven_write_role(self):
        for permissions in ({'admin':True,'maintain':False,'push':True},
                            {'admin':False,'maintain':True,'push':True},
                            {'admin':False,'maintain':False,'push':False}, {'push':True}):
            with self.subTest(permissions=permissions), patch.object(release,'get_json',
                    return_value=({'full_name':release.REPO,'permissions':permissions},False)) as read:
                with self.assertRaisesRegex(ValueError,'verified Pages write role'):
                    release.pages_preflight(self.session,SEARCH)
                read.assert_called_once_with(self.session,'')
                self.session.post.assert_not_called()

    def test_runtime_does_not_fall_back_to_legacy_credential(self):
        with patch.dict(release.os.environ,{'PAGES_TOKEN':'unused-legacy'},clear=True):
            with self.assertRaisesRegex(ValueError,'dedicated non-admin'):
                release.session_for_pages()

    def test_workflow_preflights_before_promotion_and_keeps_legacy_migration_fence(self):
        flow=yaml.safe_load(Path('.github/workflows/reader-v3-publish.yml').read_text())
        steps=flow['jobs']['publish']['steps']
        op=next(s for s in steps if s.get('id')=='operation')
        operation=op['run']
        self.assertTrue(any('scripts/requirements.txt PyYAML==6.0.3' in s.get('run','') for s in steps))
        self.assertLess(operation.index('reader_pages_release --preflight'),
                        operation.index('publish_reader_v3.py "${args[@]}"'))
        pf=next(s for s in steps if s.get('name')=='Publish the pinned projection to Pages')
        publish=pf['run']
        for key in ('HF_S3_ACCESS_KEY_ID','HF_S3_SECRET_ACCESS_KEY'):
            self.assertIn("vars.READER_PAGES_EXACT_ENABLED == 'true' && inputs.apply && inputs.operation == 'publish-generation'",op['env'][key])
            self.assertIn(f'secrets.{key}',op['env'][key])
        for step in (op,pf):
            self.assertIn('secrets.READER_PAGES_DISPATCH_TOKEN',step['env']['READER_PAGES_DISPATCH_TOKEN'])
        self.assertNotIn('PAGES_TOKEN',op['env'])
        probe=operation.split('if [[ "$OPERATION" == verify-pages-lane ]]; then\n',1)[1].split('\nfi',1)[0]
        self.assertEqual(probe.strip().splitlines(),['[[ "$APPLY" == false ]]',
            '  python -B -m scripts.reader_pages_release --preflight','  exit 0'])
        for key in ('HF_S3_ACCESS_KEY_ID','HF_S3_SECRET_ACCESS_KEY','READER_PAGES_DISPATCH_TOKEN'):
            self.assertIn("inputs.operation == 'verify-pages-lane' && !inputs.apply",op['env'][key])
        self.assertLess(publish.index('check-page-window'),publish.index('reader_pages_release --publication'))
        self.assertIn('"$OPERATION" == unify || "$OPERATION" == publish-generation',publish)
        self.assertIn('else\n  python scripts/publish_search_reader_index.py --pinned-only',publish)

    def test_claim_write_has_create_only_condition_and_no_automatic_retry(self):
        client=Mock()
        client.put_object.side_effect=TimeoutError('conditional write outcome unknown')
        with self.assertRaises(TimeoutError):release.put_claim_once(client,'bucket','key',{'version':1})
        client.put_object.assert_called_once()
        self.assertEqual(client.put_object.call_args.kwargs['IfNoneMatch'],'*')
        client.get_object.assert_not_called()


if __name__=='__main__':
    unittest.main()
