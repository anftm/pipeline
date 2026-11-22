import json
from pathlib import Path
import unittest

from scripts import reader_exact_office_plan as plan


def fixtures():
    rows=[]
    update={}
    for ext,(identity,_,__,___) in plan.PINS.items():
        old=plan.root(ext,False)
        new=plan.root(ext,True)
        for n, repo in enumerate(sorted(plan.REPOS)):
            path=f'dir/{plan.TITLE}.{ext}'
            rows.append(dict(repo=repo,path=path,key=repo+'\0'+path,source_extension=ext,
                             source_revision=str(n)*40,source_sha256=identity,profile=plan.PROFILE,root=old,
                             manifest=old+'/chapter-manifest.json',search_index=old+'/epub-search-index.json.gz',
                             processing_roots=['preserve'],unused=n))
        update[ext]=dict(root=new, manifest=new+'/chapter-manifest.json',search_index=new+'/epub-search-index.json.gz')
    parent=dict(kind='ebook-chapter-stream-index',version=1,files=rows+[{'key':'unrelated','extension':'epub'}],
                failures=[{'key':'unrelated-failure'}])
    return parent,update


class ExactOfficePlanTests(unittest.TestCase):
    def plan(self, parent, changes):
        raw=json.dumps(parent).encode()
        return plan.exact_rows(raw,plan.digest(raw),changes)

    def test_four_only(self):
        parent,changes=fixtures()
        result,old,new=self.plan(parent,changes)
        self.assertEqual(len(old),4)
        self.assertEqual(set(old),set(new))
        self.assertEqual(result['files'][-1],parent['files'][-1])
        self.assertEqual(result['failures'],parent['failures'])
        for key,row in old.items():
            self.assertEqual({k for k in row if row[k]!=new[key][k]},set(plan.FIELDS))
        self.assertEqual(len(plan.retention(old,changes)['resources']),4)

    def test_refuse_subset_drift_or_failed_record(self):
        parent,changes=fixtures()
        parent['files'].pop(0)
        with self.assertRaisesRegex(ValueError,'exactly four'):
            self.plan(parent,changes)
        parent,changes=fixtures()
        parent['failures'].append({'key':parent['files'][0]['key']})
        with self.assertRaisesRegex(ValueError,'row drift'):
            self.plan(parent,changes)
        parent,changes=fixtures()
        changes['docx']['search_index']='wrong'
        with self.assertRaisesRegex(ValueError,'successor'):
            self.plan(parent,changes)

    def test_workflow_is_readonly_and_pinned(self):
        workflow=(Path(__file__).parents[1]/'.github/workflows/reader-v3-publish.yml').read_text()
        case=workflow.split('if [[ "$OPERATION" == verify-exact-office-report ]]; then',1)[1].split('\n          fi',1)[0]
        self.assertIn('"$APPLY" == false',case)
        self.assertIn('gh api',case)
        self.assertIn('reader_exact_office_probe',case)
        self.assertIn('exit 0',case)
