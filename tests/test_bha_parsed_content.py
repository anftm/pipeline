import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import bha_parsed_content as content


class ContentReviewTests(unittest.TestCase):
    def test_ids_match_backend_layout_and_reject_collisions_or_orphan_tags(self):
        files={'abc/book/book.metadata':'a','abc/book/000/article-long.json':'b'}
        self.assertEqual(content.article_ids(files,3),{'3:12:article-long:book':'abc/book/000/article-long.json'})
        with self.assertRaisesRegex(ValueError,'duplicate'):
            content.article_ids({**files,'abc/book/001/article-long.json':'c'},3)
        with self.assertRaisesRegex(ValueError,'no indexed'):
            content.article_ids({**files,'abc/book/001/missing.tags':'c'},3)
        with self.assertRaisesRegex(ValueError,'lacks publication'):
            content.article_ids({'abc/book/000/article.json':'b'},3)

    def test_real_trees_report_article_deletion_and_tags_without_conflating_counts(self):
        with tempfile.TemporaryDirectory(prefix='bha-content-contract-') as directory:
            root=Path(directory);env={**os.environ,'GIT_AUTHOR_NAME':'vomebook','GIT_AUTHOR_EMAIL':'vomebook@gmail.com',
                'GIT_COMMITTER_NAME':'vomebook','GIT_COMMITTER_EMAIL':'vomebook@gmail.com'}
            def git(*args):return subprocess.check_output(['git',*args],cwd=root,env=env,stderr=subprocess.DEVNULL,text=True).strip()
            def write(path,value):
                p=root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value))
            git('init','-b','parsed');write('abc/book/book.metadata',{'name':'book'})
            write('abc/book/000/old.json',{'title':'old','tags':[{'name':'embedded'}]})
            write('abc/book/000/old.tags',[{'name':'previous'}]);git('add','.');git('commit','-m','before');before=git('rev-parse','HEAD^{tree}')
            (root/'abc/book/000/old.json').unlink();(root/'abc/book/000/old.tags').unlink()
            write('abc/book/000/new.json',{'title':'new'});write('abc/book/000/new.tags',[])
            git('add','-A');after=git('write-tree')
            proof=content.inspect(root,before,after,3,env)
            self.assertEqual(proof['before_documents'],proof['documents'])
            self.assertEqual(proof['removed_document_ids'],['3:3:old:book'])
            self.assertEqual(proof['added_document_ids'],['3:3:new:book'])
            self.assertEqual(proof['changed_tag_paths'],['abc/book/000/new.tags','abc/book/000/old.tags'])
            self.assertTrue(proof['json_blobs_verified']);self.assertFalse(proof['consumer_verified'])
            write('abc/book/000/new.tags',['not-an-object']);git('add','-A')
            with self.assertRaisesRegex(ValueError,'contain objects'):content.inspect(root,before,git('write-tree'),3,env)

    def test_missing_sidecar_is_explicit_and_metadata_only_tree_is_zero(self):
        self.assertEqual(content.article_ids({'abc/book/book.metadata':'a'},0),{})
        self.assertEqual(content.article_ids({'README.md':'a'},0),{})
