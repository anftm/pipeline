"""Verify exact public replacements after the daily controller's profile batch."""
import argparse,json
from pathlib import Path
import httpx
from .reader_web_batch_cli import live
from .publish_reader_v3 import encode


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('publication','consumers','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();result=json.loads(a.publication.read_bytes()); profile=result['profile']; n=result['changed_rows']
    if type(n) is not int or n!=profile['converted'] or n!=len(profile['rows']):
        raise ValueError('profile readback scope differs')
    with httpx.Client(follow_redirects=True,timeout=30) as http:
        out=live({'target_pointer':{'generation':result['generation']},'rows':profile['rows']},
                 json.loads(a.consumers.read_bytes()),result,http)
    with a.output.open('xb') as f:f.write(encode(out))

if __name__=='__main__':main()
