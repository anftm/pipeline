"""Authenticate successful staging even when a later activation step failed."""
import argparse
import json
from pathlib import Path


def validate(run, jobs, *, run_id, sha):
    if (not isinstance(run, dict) or run.get('id') != run_id or run.get('status') != 'completed'
            or run.get('conclusion') not in {'success','failure','timed_out','cancelled'}
            or run.get('head_branch') != 'main' or run.get('head_sha') != sha
            or run.get('event') not in {'schedule','workflow_dispatch'}
            or run.get('path','').split('@',1)[0] != '.github/workflows/publish-ccrd-index.yml'
            or run.get('run_attempt') != 1 or type(run.get('run_attempt')) is not int):
        raise ValueError('staging run identity or attempt differs')
    if (not isinstance(jobs, dict) or jobs.get('total_count') != 1
            or not isinstance(jobs.get('jobs'), list) or len(jobs['jobs']) != 1):
        raise ValueError('complete single staging job required')
    job = jobs['jobs'][0]
    if job.get('run_id') != run_id or job.get('head_sha') != sha or job.get('name') != 'stage':
        raise ValueError('foreign staging job')
    steps = job.get('steps')
    if not isinstance(steps, list):raise ValueError('staging steps missing')
    required = ['Build and stage only if the upstream source changed',
                'Preserve exact staged candidate for recovery']
    numbers=[]
    for name in required:
        selected=[s for s in steps if s.get('name')==name]
        if (len(selected)!=1 or selected[0].get('conclusion')!='success'
                or selected[0].get('status')!='completed' or type(selected[0].get('number')) is not int):
            raise ValueError('exact staging and candidate upload must succeed')
        numbers.append(selected[0]['number'])
    if numbers != sorted(set(numbers)):
        raise ValueError('staging artifact order differs')
    return {'run':run_id,'sha':sha,'staging_verified':True,'publication_conclusion':run['conclusion']}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--jobs',type=Path,required=True)
    p.add_argument('--run-id',type=int,required=True);p.add_argument('--sha',required=True)
    a=p.parse_args(argv)
    print(json.dumps(validate(json.loads(a.run.read_bytes()),json.loads(a.jobs.read_bytes()),run_id=a.run_id,sha=a.sha)))


if __name__=='__main__':main()
