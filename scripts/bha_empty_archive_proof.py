"""Read-only source-tree and authenticated serving proof for empty BHA archives.

This proves absence for the selected exact source trees and serving generation.
It does not activate an index, grant deletion authority or prove nonempty content.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from . import check_bha_revisions as revisions
    from . import check_bha_reindex_access as access
    from . import search_snapshot
except ImportError:
    import check_bha_revisions as revisions
    import check_bha_reindex_access as access
    import search_snapshot

SHA = re.compile(r'[0-9a-f]{40}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
ARCHIVES = (6, 7, 13, 26)
MAX_TREE = 16 * 1024 * 1024
MAX_PROOF = 256 * 1024
BASE = 'https://vomebook-bha-search.hf.space'


def read_json(url, token, limit, *, open_url=urlopen):
    request = Request(url, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json', 'Cache-Control': 'no-cache',
        'User-Agent': 'pipeline-bha-empty-archive-proof'}, method='GET')
    try:
        with open_url(request, timeout=60) as response:
            if response.status != 200:
                raise RuntimeError('proof read did not succeed')
            raw = response.read(limit + 1)
    except HTTPError as error:
        code = error.code
        error.close()
        raise RuntimeError('proof read HTTP ' + str(code)) from None
    except (URLError, OSError):
        raise RuntimeError('proof read transport failed') from None
    if len(raw) > limit:
        raise ValueError('proof response exceeds bound')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('proof response must be an object')
    return value


def empty_tree(value, tree):
    if (not isinstance(value, dict) or value.get('sha') != tree
            or value.get('truncated') is not False or not isinstance(value.get('tree'), list)
            or len(value['tree']) > 100000):
        raise ValueError('source tree identity or complete inventory differs')
    seen = set()
    directories = set()
    metadata = 0
    for entry in value['tree']:
        if not isinstance(entry, dict):
            raise ValueError('invalid tree entry')
        path, kind, mode, digest = (entry.get(k) for k in ('path','type','mode','sha'))
        if (not isinstance(path, str) or not path or path in seen or path.startswith('/')
                or '\\' in path or any(p in {'','.','..'} for p in path.split('/'))
                or any(ord(c) < 32 for c in path) or not isinstance(digest, str) or not SHA.fullmatch(digest)
                or (kind, mode) not in {('tree','040000'),('blob','100644'),('blob','100755')}):
            raise ValueError('unsupported or duplicate source tree entry')
        seen.add(path)
        if kind == 'tree':
            directories.add(path)
        if kind == 'blob':
            if type(entry.get('size')) is not int or entry['size'] < 0:
                raise ValueError('source blob size missing')
            # Conservative absence proof: any article JSON or tag sidecar blocks,
            # including unexpected layouts the current backend may not index.
            if path.endswith(('.json', '.tags')):
                raise ValueError('source archive contains article or tag data')
            metadata += path.endswith('.metadata')
    for path in seen:
        parts = path.split('/')
        for i in range(1, len(parts)):
            parent = '/'.join(parts[:i])
            if parent not in directories:
                raise ValueError('source tree omits parent directory')
    return {'tree': tree, 'entries': len(seen), 'metadata_files': metadata,
            'article_json_files': 0, 'tag_files': 0,
            'inventory_sha256': hashlib.sha256(search_snapshot.canonical(value)).hexdigest()}


def status_identity(value):
    if (not isinstance(value, dict) or value.get('index_status') != 'ready'
            or value.get('switch_pending') is not False):
        raise ValueError('serving index is not stably ready')
    trees = value.get('archive_revisions')
    if (not isinstance(trees, dict) or set(trees) != {str(i) for i in range(32)}
            or any(not isinstance(v, str) or not SHA.fullmatch(v) for v in trees.values())
            or not isinstance(value.get('build_fingerprint'), str)
            or not SHA256.fullmatch(value['build_fingerprint'])):
        raise ValueError('serving generation identity incomplete')
    job = value.get('job')
    if job is not None and (not isinstance(job, dict) or job.get('state') not in {'succeeded','failed'}):
        raise ValueError('index job still active or unknown')
    return {'archive_revisions': trees, 'build_fingerprint': value['build_fingerprint'], 'job': job}


def inspect(github_token, admin_token, *, archives=ARCHIVES,
            lookup=revisions.archive_identity, status=access.read_status, fetch=read_json):
    if not github_token or not admin_token:
        raise ValueError('existing GitHub and BHA credentials required')
    if (not archives or len(archives) > 32 or len(set(archives)) != len(archives)
            or any(type(a) is not int or not 0 <= a <= 31 for a in archives)):
        raise ValueError('invalid archive selection')
    before = status_identity(status(admin_token))
    rows = []
    identities = {}
    for archive in archives:
        identity = lookup(github_token, archive)
        key, commit, tree = identity
        if (key != str(archive) or not isinstance(commit, str) or not SHA.fullmatch(commit)
                or not isinstance(tree, str) or not SHA.fullmatch(tree)
                or before['archive_revisions'][key] != tree):
            raise ValueError('source and serving archive identities differ')
        identities[archive] = identity
        tree_url = f'https://api.github.com/repos/{revisions.OWNER}/banned-historical-archives{archive}/git/trees/{tree}?recursive=1'
        source = empty_tree(fetch(tree_url, github_token, MAX_TREE), tree)
        proof = fetch(BASE + '/api/reindex/archive/' + str(archive), admin_token, MAX_PROOF)
        if (proof.get('kind') != 'bha-archive-cardinality' or type(proof.get('archive')) is not int
                or proof['archive'] != archive or proof.get('build_fingerprint') != before['build_fingerprint']
                or proof.get('archive_revisions') != before['archive_revisions']
                or proof.get('all_counts_equal') is not True or type(proof.get('remote_writes')) is not int
                or proof['remote_writes'] != 0 or not isinstance(proof.get('counts'), dict)
                or set(proof['counts']) != {'elasticsearch','docs','facets'}
                or any(type(v) is not int or v != 0 for v in proof['counts'].values())):
            raise ValueError('authenticated serving proof does not confirm exact zero')
        rows.append({'archive': archive, 'commit': commit, **source, 'serving': proof})
    for archive, identity in identities.items():
        if lookup(github_token, archive) != identity:
            raise ValueError('source changed during proof')
    if status_identity(status(admin_token)) != before:
        raise ValueError('serving generation changed during proof')
    return {'version': 1, 'kind': 'bha-empty-archive-proof', **before, 'archives': rows,
            'all_empty_verified': True, 'remote_writes': 0, 'publication_ready': False,
            'scope': 'selected source trees and serving counts; no index mutation or deletion authority'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('proof output must be new')
    result = inspect(os.environ.get('GITHUB_TOKEN',''), os.environ.get('BHA_REINDEX_TOKEN',''))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
    print(json.dumps({'archives': [r['archive'] for r in result['archives']],
                      'all_empty_verified': True, 'remote_writes': 0}))


if __name__ == '__main__':
    main()
