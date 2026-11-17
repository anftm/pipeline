import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from scripts import bha_empty_archive_proof as subject


class EmptyArchiveProofTests(unittest.TestCase):
    def setUp(self):
        self.tree = 'a'*40
        self.commit = 'b'*40
        self.status = {'index_status':'ready','switch_pending':False,'job':None,
                       'archive_revisions':{str(i):self.tree for i in range(32)},'build_fingerprint':'c'*64}
        self.source = {'sha':self.tree,'truncated':False,'tree':[
            {'path':'publications','mode':'040000','type':'tree','sha':'d'*40},
            {'path':'publications/book.metadata','mode':'100644','type':'blob','sha':'e'*40,'size':2}]}
        self.proof = {'kind':'bha-archive-cardinality','archive':6,
                      'archive_revisions':self.status['archive_revisions'],'build_fingerprint':'c'*64,
                      'counts':{'elasticsearch':0,'docs':0,'facets':0},'all_counts_equal':True,'remote_writes':0}
        self.lookup = Mock(return_value=('6',self.commit,self.tree))
        self.read_status = Mock(return_value=self.status)

    def inspect(self):
        def fetch(url,token,limit):
            if url.startswith('https://api.github.com/'):
                self.assertEqual(token,'github-fixture');return self.source
            self.assertEqual(token,'admin-fixture');return self.proof
        return subject.inspect('github-fixture','admin-fixture',archives=(6,),lookup=self.lookup,
                               status=self.read_status,fetch=fetch)

    def test_complete_tree_and_three_store_zero_are_bound(self):
        report=self.inspect()
        self.assertTrue(report['all_empty_verified']);self.assertFalse(report['publication_ready'])
        self.assertEqual(report['archives'][0]['entries'],2)
        self.assertEqual(self.lookup.call_count,2);self.assertEqual(self.read_status.call_count,2)

    def test_truncated_wrong_tree_and_missing_parent_rejected(self):
        for change in [{'truncated':True},{'sha':'f'*40},{'tree':[self.source['tree'][1]]}]:
            with self.subTest(change=change),self.assertRaises(ValueError):subject.empty_tree({**self.source,**change},self.tree)

    def test_article_any_layout_and_orphan_tag_rejected(self):
        for path in ['article.json','unusual.tags']:
            tree=copy.deepcopy(self.source);tree['tree'].append({'path':path,'mode':'100644','type':'blob','sha':'e'*40,'size':2})
            with self.assertRaisesRegex(ValueError,'article or tag'):subject.empty_tree(tree,self.tree)

    def test_symlink_submodule_duplicate_and_missing_size_rejected(self):
        for change in [{'mode':'120000'},{'type':'commit','mode':'160000'},{'size':None},{'path':'../bad'}]:
            tree=copy.deepcopy(self.source);tree['tree'][1].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):subject.empty_tree(tree,self.tree)
        tree=copy.deepcopy(self.source);tree['tree'].append(tree['tree'][0])
        with self.assertRaises(ValueError):subject.empty_tree(tree,self.tree)

    def test_nonzero_partial_or_wrong_generation_proof_rejected(self):
        original=copy.deepcopy(self.proof)
        for change in [{'counts':{'elasticsearch':0,'docs':1,'facets':0}}, {'counts':{'elasticsearch':False,'docs':0,'facets':0}},
                       {'build_fingerprint':'f'*64},{'archive':7},{'archive':True},
                       {'all_counts_equal':False},{'archive_revisions':{}},{'remote_writes':1}]:
            self.proof={**original,**change}
            with self.subTest(change=change),self.assertRaises(ValueError):self.inspect()

    def test_moving_source_and_serving_state_rejected(self):
        self.lookup.side_effect=[('6',self.commit,self.tree),('6','f'*40,self.tree)]
        with self.assertRaisesRegex(ValueError,'source changed'):self.inspect()
        self.lookup.side_effect=None
        self.read_status.side_effect=[self.status,{**self.status,'build_fingerprint':'f'*64}]
        with self.assertRaisesRegex(ValueError,'serving generation changed'):self.inspect()

    def test_active_job_and_mismatched_source_rejected_before_count(self):
        self.status['job']={'state':'running'}
        with self.assertRaisesRegex(ValueError,'active'):self.inspect()
        self.status['job']=None;self.lookup.return_value=('6',self.commit,'f'*40)
        with self.assertRaisesRegex(ValueError,'identities differ'):self.inspect()

    def test_read_bound_and_get_method(self):
        class Response(io.BytesIO):status=200
        opened=Mock(return_value=Response(b'{"ok":true}'))
        self.assertEqual(subject.read_json('https://example.test','fixture',20,open_url=opened),{'ok':True})
        self.assertEqual(opened.call_args.args[0].method,'GET')
        with self.assertRaisesRegex(ValueError,'bound'):
            subject.read_json('https://example.test','fixture',2,open_url=lambda *a,**k:Response(b'123'))

    def test_workflow_has_no_index_or_bucket_write_credentials(self):
        raw=(Path(__file__).parents[1]/'.github/workflows/bha-empty-archive-proof.yml').read_text()
        self.assertIn('contents: read',raw);self.assertIn('group: update-bha-index',raw)
        self.assertIn('secrets.BHA_REINDEX_TOKEN',raw)
        for forbidden in ['HF_TOKEN','HF_S3','--apply','git push','actions: write']:
            self.assertNotIn(forbidden,raw)


if __name__=='__main__':unittest.main()
