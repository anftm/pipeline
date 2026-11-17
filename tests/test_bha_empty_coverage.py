import copy
import hashlib
import unittest
from unittest.mock import Mock
from scripts import bha_empty_coverage as empty, bha_reindex_run as runner
from scripts import bha_reindex_delivery as delivery, bha_reindex_journal as journal, search_snapshot
from tests.test_bha_reindex_delivery import plan, Store, receipt, live_status, snapshot_report, snapshot_health, T


def source_row(value, archive=6):
    return {'archive':archive,'commit':value['archive_commits'][str(archive)],
            'tree':value['archive_revisions'][str(archive)],'entries':1,'metadata_files':0,
            'article_json_files':0,'tag_files':0,'inventory_sha256':'f'*64}


def served_row(value):
    row=source_row(value)
    row['serving']={'kind':'bha-archive-cardinality','archive':6,'build_fingerprint':'d'*64,
                    'archive_revisions':value['archive_revisions'],'all_counts_equal':True,'remote_writes':0,
                    'counts':dict.fromkeys(['elasticsearch','docs','facets'],0)}
    return row


class EmptyCoverageTests(unittest.TestCase):
    def target(self):
        p=plan();p['archive_revisions']['6']='c'*40;p['changed_archives']=[0,6]
        p['post_index_sample_missing_archives']=[];p['empty_source_proofs']=[source_row(p)]
        p['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in p.items() if k!='plan_sha256'})).hexdigest()
        return p

    def test_coverage_requires_bound_empty_source(self):
        p=self.target();runner.require_coverage([0,6],[{'archive':0}],p)
        for field,value in [('tree','e'*40),('article_json_files',1),('tag_files',True)]:
            wrong=copy.deepcopy(p);wrong['empty_source_proofs'][0][field]=value
            with self.assertRaises(ValueError):runner.require_coverage([0,6],[{'archive':0}],wrong)
        with self.assertRaises(ValueError):runner.require_coverage([0,6],[{'archive':0}])

    def test_pinned_empty_source_is_commit_bound_no_moving_head_lookup(self):
        p=self.target();commit=p['archive_commits']['6'];tree=p['archive_revisions']['6']
        fetch=Mock(side_effect=[{'sha':commit,'tree':{'sha':tree}}, {'sha':tree,'truncated':False,'tree':[]}])
        row=empty.source(6,commit,tree,'fixture',fetch=fetch)
        self.assertEqual(row['article_json_files'],0)
        self.assertTrue(fetch.call_args_list[0].args[0].endswith('/commits/'+commit))
        fetch=Mock(return_value={'sha':commit,'tree':{'sha':'e'*40}})
        with self.assertRaisesRegex(ValueError,'binding'):empty.source(6,commit,tree,'fixture',fetch=fetch)

    def accepted_store(self,p):
        s=Store();delivery.prepare(s,plan=p,mode='incremental',inspect_live=lambda:p,operation=T)
        send=Mock(return_value=receipt(p));delivery.apply_once(s,operation=T,token='fixture',inspect_live=lambda:p,post=send)
        return s,send

    def test_real_journal_accepts_empty_changed_archive_with_snapshot(self):
        p=self.target();s,send=self.accepted_store(p)
        samples={'consumer_health_observed_twice':True,'remote_writes':0,
                 'samples':[{'archive':0,'commit':p['archive_commits']['0'],'tree':p['archive_revisions']['0'],
                             'preview_article_equal':True,'exact_title_search_found':True}],
                 'empty_archives':[served_row(p)]}
        result=delivery.reconcile(s,operation=T,token='fixture',status=lambda _:live_status(p),
            verify_samples=lambda:samples,verify_snapshot=snapshot_report,fetch=snapshot_health)
        self.assertEqual(result['status'],'verified');send.assert_called_once()
        self.assertEqual(journal.load(s).record['result']['empty_archives'],samples['empty_archives'])

    def test_fingerprint_change_after_zero_proof_cannot_use_new_snapshot(self):
        p=self.target();s,_=self.accepted_store(p)
        samples={'consumer_health_observed_twice':True,'remote_writes':0,
            'samples':[{'archive':0,'commit':p['archive_commits']['0'],'tree':p['archive_revisions']['0'],
                        'preview_article_equal':True,'exact_title_search_found':True}],
            'empty_archives':[served_row(p)]}
        status=Mock(side_effect=[live_status(p),{**live_status(p),'build_fingerprint':'e'*64}])
        snapshot=Mock()
        with self.assertRaisesRegex(ValueError,'consumer changed'):
            delivery.reconcile(s,operation=T,token='fixture',status=status,
                verify_samples=lambda:samples,verify_snapshot=snapshot,fetch=snapshot_health)
        snapshot.assert_not_called();self.assertEqual(journal.load(s).record['state'],'accepted')

    def test_nonzero_stale_or_missing_empty_proof_does_not_finish_journal(self):
        p=self.target()
        for kind in ['nonzero','stale','missing','fingerprint']:
            s,_=self.accepted_store(p);row=served_row(p)
            if kind=='nonzero':row['serving']['counts']['docs']=1
            if kind=='stale':row['serving']['archive_revisions']={**p['archive_revisions'],'6':'e'*40}
            if kind=='fingerprint':row['serving']['build_fingerprint']='e'*64
            samples={'consumer_health_observed_twice':True,'remote_writes':0,
                'samples':[{'archive':0,'commit':p['archive_commits']['0'],'tree':p['archive_revisions']['0'],
                            'preview_article_equal':True,'exact_title_search_found':True}],
                'empty_archives':[] if kind=='missing' else [row]}
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                delivery.reconcile(s,operation=T,token='fixture',status=lambda _:live_status(p),
                    verify_samples=lambda:samples,verify_snapshot=snapshot_report,fetch=snapshot_health)
            self.assertEqual(journal.load(s).record['state'],'accepted')
