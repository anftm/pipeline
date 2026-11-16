import unittest
from pathlib import Path
import yaml


class CandidateWorkflowTests(unittest.TestCase):
    def test_baseline_requires_exact_publication_and_never_pushes(self):
        value=yaml.safe_load(Path('.github/workflows/bha-baseline-handoff.yml').read_text())
        self.assertEqual(value['permissions'],{'contents':'read','actions':'read'})
        commands='\n'.join(s.get('run','') for s in value['jobs']['prepare']['steps'])
        self.assertIn('scripts.bha_parsed_baseline',commands)
        self.assertIn('.github/workflows/bha-publish-candidate.yml',commands)
        self.assertNotIn('git push',commands)
        self.assertNotIn('/api/reindex',commands)
        inputs=value.get('on',value.get(True))['workflow_dispatch']['inputs']
        self.assertIs(inputs['commit_baseline']['default'],False)
        step=next(s for s in value['jobs']['prepare']['steps'] if s.get('name')=='Commit exact baseline pair after fresh acceptance')
        self.assertEqual(step['if'],'inputs.commit_baseline == true')
        self.assertIn('scripts.bha_baseline_publish',step['run'])
        self.assertIn('--expected-sha "$GITHUB_SHA"',step['run'])

    def test_publisher_requires_exact_candidate_provenance_without_rebuild(self):
        value=yaml.safe_load(Path('.github/workflows/bha-publish-candidate.yml').read_text())
        self.assertEqual(set(value.get('on',value.get(True))),{'workflow_dispatch'})
        self.assertEqual(value['concurrency']['group'],'maintain-archive-forks')
        commands='\n'.join(s.get('run','') for s in value['jobs']['publish']['steps'])
        self.assertIn('scripts.bha_parsed_publish',commands)
        self.assertIn(".github/workflows/bha-build-candidate.yml",commands)
        self.assertIn("r.get('head_sha')!=os.environ['GITHUB_SHA']",commands)
        self.assertIn('"$GITHUB_RUN_ATTEMPT" == 1',commands)
        self.assertNotIn('build_archive',commands)
        self.assertNotIn('/api/reindex',commands)

    def test_candidate_is_manual_serialized_without_remote_publish(self):
        value=yaml.safe_load(Path('.github/workflows/bha-build-candidate.yml').read_text())
        self.assertEqual(set(value.get('on',value.get(True))),{'workflow_dispatch'})
        self.assertEqual(value['permissions'],{'contents':'read','actions':'read'})
        self.assertEqual(value['concurrency']['group'],'maintain-archive-forks')
        self.assertIs(value['concurrency']['cancel-in-progress'],False)
        steps=value['jobs']['candidate']['steps'];commands='\n'.join(s.get('run','') for s in steps)
        self.assertIn('scripts.bha_parsed_candidate',commands)
        for forbidden in ['git push','publish_candidate','build_archive','/api/reindex']:
            self.assertNotIn(forbidden,commands)
        self.assertIn("r.get('head_sha')!=os.environ['GITHUB_SHA']",commands)
        self.assertIn(".github/workflows/bha-build-plan.yml",commands)
        artifact=next(s for s in steps if s.get('uses')=='actions/upload-artifact@v7')
        self.assertEqual(artifact['if'],'always()');self.assertEqual(artifact['with']['retention-days'],7)
        build=next(s for s in steps if s.get('name')=='Build local candidate without publishing')
        self.assertNotIn('${{',build['run'])
