"""GET-only preflight for the surviving producer-fence GitHub credentials.

Owner API credentials check deleted repository identities, not the HF/S3 keys
that old workflows used. This tool is diagnostic and cannot authorize GC.
"""
from __future__ import annotations

import json
import os
import re

import httpx

try:
    from . import reader_gc_credentials as credentials, reader_producer_v2_names as names
except ImportError:
    import reader_gc_credentials as credentials
    import reader_producer_v2_names as names


CENTRAL = 'anftm/pipeline'
WORKFLOW = CENTRAL + '/.github/workflows/producer-v2-credential-preflight.yml@refs/heads/main'
STATUSES = ('in_progress', 'queued', 'pending', 'waiting', 'requested')


def require_context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != CENTRAL
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW
            or not re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', ''))):
        raise RuntimeError('producer v2 credential check requires its central workflow')


def central_check(*, get=None, token_for=credentials.token_for):
    get = get or httpx.get
    token = token_for(CENTRAL)
    if not isinstance(token, str) or not token:
        raise RuntimeError('central producer credential unavailable')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'User-Agent': 'reader-producer-v2-credential-preflight'}
    def fetch(path, params=None):
        try:
            response = get('https://api.github.com' + path, headers=headers, params=params,
                           timeout=30, follow_redirects=False)
        except httpx.TransportError as error:
            raise RuntimeError('central producer credential transport failed') from error
        if response.status_code != 200:
            raise RuntimeError('central producer credential GET failed: HTTP ' + str(response.status_code))
        return response.json()
    identity = fetch('/repos/' + CENTRAL)
    if identity.get('id') != 1260985830 or identity.get('full_name') != CENTRAL:
        raise RuntimeError('central producer identity differs')
    workflows = fetch('/repos/' + CENTRAL + '/actions/workflows', {'per_page': 1})
    if type(workflows.get('total_count')) is not int or workflows['total_count'] < 1:
        raise RuntimeError('central producer workflow inventory unavailable')
    counts = {}
    for status in STATUSES:
        runs = fetch('/repos/' + CENTRAL + '/actions/runs', {'status': status, 'per_page': 1})
        if type(runs.get('total_count')) is not int or runs['total_count'] < 0:
            raise RuntimeError('central producer run inventory unavailable')
        counts[status] = runs['total_count']
    return {'repository': CENTRAL, 'repository_id': identity['id'],
            'workflow_count': workflows['total_count'], 'run_counts': counts}


def run():
    require_context()
    central = central_check()
    owners = names.check()
    return {'kind': 'reader-producer-v2-credential-preflight', 'central': central,
            'retired_owners': owners['repositories'], 'scope_activated': False,
            'source_credentials_revoked': False, 'external_writers_fenced': False,
            'remote_mutations': 0, 'gc_authorized': False}


def main():
    print(json.dumps(run(), sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
