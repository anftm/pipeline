import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
from scripts import bha_baseline_publish as publisher, search_snapshot


class BaselinePublishTests(unittest.TestCase):
    def test_real_two_file_commit_and_single_lease_preserve_other_archive(self):
        with tempfile.TemporaryDirectory(prefix='bha-baseline-git-') as directory:
            repo=Path(directory);(repo/'state').mkdir()
            env={**os.environ,'GIT_AUTHOR_NAME':'vomebook','GIT_AUTHOR_EMAIL':'vomebook@gmail.com',
                 'GIT_COMMITTER_NAME':'vomebook','GIT_COMMITTER_EMAIL':'vomebook@gmail.com'}
            def git(*args):return subprocess.check_output(['git',*args],cwd=repo,env=env,stderr=subprocess.DEVNULL,text=True).strip()
            before_inputs={'version':1,'archives':{'0':{'main':'a'},'1':{'main':'b'}}};before_trees={'0':'a','1':'b'}
            for path,value in zip(publisher.FILES,[before_inputs,before_trees]):(repo/path).write_text(json.dumps(value))
            (repo/'unrelated.txt').write_text('keep');git('init','-b','main');git('add','.');git('commit','-m','before');parent=git('rev-parse','HEAD')
            after_inputs=copy.deepcopy(before_inputs);after_inputs['archives']['0']['main']='c';after_trees={**before_trees,'0':'c'}
            def sha(value):return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()
            candidate={'kind':'bha-parsed-baseline-candidate','baseline_published':False,'archive':0,'operation':'f'*32,
                'inputs':after_inputs,'trees':after_trees,'before_inputs_sha256':sha(before_inputs),
                'before_trees_sha256':sha(before_trees),'inputs_sha256':sha(after_inputs),'trees_sha256':sha(after_trees)}
            head=[parent];pushes=[];saved=[]
            def push(commit,lease):
                self.assertEqual(lease,parent);self.assertEqual(saved[-1]['status'],'push-intent')
                pushes.append(commit);head[0]=commit
            result=publisher.publish(repo,candidate,parent,revalidate=lambda:copy.deepcopy(candidate),
                check_window=lambda:None,remote_head=lambda:head[0],push=push,checkpoint=saved.append)
            self.assertTrue(result['baseline_published']);self.assertEqual(len(pushes),1)
            self.assertEqual(set(git('diff','--name-only',parent,'HEAD').splitlines()),set(publisher.FILES))
            self.assertEqual(json.loads((repo/publisher.FILES[0]).read_text())['archives']['1'],{'main':'b'})
            self.assertEqual(git('show','-s','--format=%an <%ae>|%cn <%ce>'),
                             'vomebook <vomebook@gmail.com>|vomebook <vomebook@gmail.com>')

    def test_active_reader_blocks_before_any_file_change(self):
        with tempfile.TemporaryDirectory(prefix='bha-baseline-block-') as directory:
            repo=Path(directory);(repo/'state').mkdir();values=[{'version':1,'archives':{'0':{'main':'a'}}},{'0':'a'}]
            for path,value in zip(publisher.FILES,values):(repo/path).write_text(json.dumps(value))
            sha=lambda v:hashlib.sha256(search_snapshot.canonical(v)).hexdigest()
            candidate={'kind':'bha-parsed-baseline-candidate','baseline_published':False,'archive':0,'inputs':values[0],'trees':values[1],
                       'before_inputs_sha256':sha(values[0]),'before_trees_sha256':sha(values[1]),
                       'inputs_sha256':sha(values[0]),'trees_sha256':sha(values[1])}
            git=Mock(side_effect=['a'*40,'']);push=Mock();checkpoint=Mock()
            with self.assertRaisesRegex(ValueError,'active'):
                publisher.publish(repo,candidate,'a'*40,revalidate=lambda:candidate,
                    check_window=Mock(side_effect=ValueError('Reader active')),remote_head=lambda:'a'*40,
                    push=push,checkpoint=checkpoint,git=git)
            push.assert_not_called();checkpoint.assert_not_called();self.assertEqual(git.call_count,2)
