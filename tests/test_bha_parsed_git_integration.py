"""Exercise real orphan bundles and leased pushes using tiny local repositories."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import build_fork_parsed as builder, bha_parsed_publish as publisher


class GitIntegrationTests(unittest.TestCase):
    def test_reviewed_orphan_bundle_roundtrip_and_exact_leased_push(self):
        # Owned temporary fixture, under 1MB, no network, removed after test.
        with tempfile.TemporaryDirectory(prefix='bha-git-contract-') as directory:
            root=Path(directory); source=root/'source'; remote=root/'remote.git'
            env={**os.environ,'GIT_AUTHOR_NAME':'vomebook','GIT_AUTHOR_EMAIL':'vomebook@gmail.com',
                 'GIT_COMMITTER_NAME':'vomebook','GIT_COMMITTER_EMAIL':'vomebook@gmail.com',
                 'GIT_CONFIG_NOSYSTEM':'1'}
            def git(*args,cwd=source):
                return subprocess.check_output(['git',*args],cwd=cwd,env=env,stderr=subprocess.DEVNULL,text=True).strip()
            source.mkdir();git('init','-b','parsed');(source/'old.json').write_text('{"old":true}')
            git('add','.');git('commit','-m','original');before=git('rev-parse','HEAD');before_tree=git('rev-parse','HEAD^{tree}')
            git('clone','--bare',str(source),str(remote),cwd=root)
            git('checkout','--orphan','candidate');git('reset','--hard')
            (source/'new.json').write_text('{"new":true}');git('add','.');git('commit','-m','candidate')
            commit=git('rev-parse','HEAD');tree=git('rev-parse','HEAD^{tree}')
            diff=git('diff','--name-status','--no-renames',before,tree)
            bundle=root/'parsed.bundle';git('bundle','create',str(bundle),'HEAD')
            candidate={'version':1,'kind':'bha-parsed-local-candidate','archive':0,'helper_revision':'b'*40,
                'revisions':{k:before for k in (*builder.INPUT_BRANCHES,builder.PARSED_BRANCH)},
                'before_tree':before_tree,'tree':tree,'commit':commit,'changed':True,
                'diff_name_status':diff,'remote_writes':0}
            restored=publisher.restore_bundle(root/'restore',candidate,bundle,'fixture',source_url=str(remote))
            self.assertEqual(git('rev-parse',commit+'^{tree}',cwd=restored),tree)
            self.assertEqual(git('rev-parse',before+'^{tree}',cwd=restored),before_tree)
            actual_run=builder.run; checkpoints=[]; pushes=[]
            def local_run(command,**kwargs):
                command=list(command)
                if 'push' in command:
                    pushes.append(command)
                    url=f'https://github.com/{builder.MIRROR_OWNER}/{builder.REPOSITORY_PREFIX}0.git'
                    command[command.index(url)]=str(remote)
                actual_run(command,**kwargs)
            def ref(_token,_owner,repo,branch):
                return 'b'*40 if repo=='ocr_helper' else git('rev-parse','refs/heads/'+branch,cwd=remote)
            with patch.object(builder,'branch_revisions',return_value=candidate['revisions']), \
                    patch.object(builder,'ref_revision',side_effect=ref), \
                    patch.object(builder,'run',side_effect=local_run):
                self.assertTrue(builder.publish_candidate('fixture',root/'restore',candidate,checkpoint=checkpoints.append))
            self.assertEqual(len(pushes),1);self.assertEqual(git('rev-parse','parsed',cwd=remote),commit)
            self.assertEqual([x['status'] for x in checkpoints],['push-intent','published'])
            self.assertIn('D\told.json',diff);self.assertIn('A\tnew.json',diff)
