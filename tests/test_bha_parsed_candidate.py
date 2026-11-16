import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from scripts import build_fork_parsed as builder


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {'version':1,'kind':'bha-parsed-local-candidate','archive':0,
            'revisions':{k:'a'*40 for k in (*builder.INPUT_BRANCHES,builder.PARSED_BRANCH)},
            'helper_revision':'b'*40,'before_tree':'c'*40,'tree':'d'*40,'commit':'e'*40,
            'changed':True,'remote_writes':0,'diff_name_status':'M\tarticle.json'}
        self.push = Mock()
        self.branches = Mock(return_value=self.candidate['revisions'])
        self.refs = Mock(side_effect=lambda token,owner,repo,branch: 'b'*40 if repo=='ocr_helper' else 'e'*40)
        def output(args, **kw):
            if args[1] == 'rev-parse':
                return self.candidate['before_tree'] if args[2].startswith('a'*40) else self.candidate['tree']
            if args[1] == 'diff': return self.candidate['diff_name_status']
            return 'vomebook <vomebook@gmail.com>|vomebook <vomebook@gmail.com>'
        self.output = Mock(side_effect=output)
        for name, mock in [('run',self.push),('branch_revisions',self.branches),('ref_revision',self.refs),('run_output',self.output)]:
            context=patch.object(builder,name,mock);context.start();self.addCleanup(context.stop)
        context=patch.object(builder,'push_environment',return_value={});context.start();self.addCleanup(context.stop)
        self.checkpoint=Mock()

    def publish(self):
        return builder.publish_candidate('token',Path('/tmp/candidate-test-not-created'),self.candidate,checkpoint=self.checkpoint)

    def test_exact_commit_lease_and_checkpoint_precede_single_push(self):
        self.push.side_effect=lambda *a,**kw:self.assertEqual(self.checkpoint.call_args.args[0]['status'],'push-intent')
        self.assertTrue(self.publish())
        command=self.push.call_args.args[0]
        self.assertIn('e'*40+':refs/heads/parsed',command)
        self.assertIn('--force-with-lease=refs/heads/parsed:'+'a'*40,command)
        self.push.assert_called_once()
        self.assertEqual(self.checkpoint.call_args.args[0]['status'],'published')

    def test_source_change_during_checkpoint_stops_push(self):
        self.checkpoint.side_effect=lambda value:setattr(self.branches,'return_value',{**self.candidate['revisions'],'main':'f'*40})
        with self.assertRaisesRegex(RuntimeError,'inputs changed'):self.publish()
        self.push.assert_not_called()

    def test_lost_response_recognizes_exact_commit_without_replay(self):
        self.push.side_effect=OSError('lost')
        self.assertTrue(self.publish());self.push.assert_called_once()
        self.assertEqual(self.checkpoint.call_args.args[0]['push_error'],'OSError')

    def test_unknown_outcome_stops_and_records_attempt(self):
        self.push.side_effect=OSError('lost')
        self.refs.side_effect=lambda token,owner,repo,branch:'b'*40 if repo=='ocr_helper' else 'a'*40
        with self.assertRaisesRegex(RuntimeError,'unconfirmed'):self.publish()
        self.push.assert_called_once();self.assertEqual(self.checkpoint.call_args.args[0]['status'],'unknown')

    def test_noop_and_invalid_candidate_never_push(self):
        self.candidate.update(changed=False,tree='c'*40,commit='a'*40)
        self.assertFalse(self.publish());self.push.assert_not_called()
        self.candidate['tree']='f'*40
        with self.assertRaises(ValueError):self.publish()

    def test_reviewed_diff_change_blocks_before_push(self):
        prior=self.output.side_effect
        self.output.side_effect=lambda args,**kw:'D\tunexpected.json' if args[1]=='diff' else prior(args,**kw)
        with self.assertRaisesRegex(ValueError,'reviewed changes'):self.publish()
        self.push.assert_not_called()

    def test_checkpoint_failure_prevents_push(self):
        self.checkpoint.side_effect=OSError('cannot persist intent')
        with self.assertRaises(OSError):self.publish()
        self.push.assert_not_called()

    def test_wrong_commit_identity_blocks(self):
        prior=self.output.side_effect
        self.output.side_effect=lambda args,**kw:'wrong' if args[1]=='show' else prior(args,**kw)
        with self.assertRaisesRegex(ValueError,'author/committer'):self.publish()
        self.push.assert_not_called()
