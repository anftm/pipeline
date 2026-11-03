"""Observe retired repository names with the exact owner credential.

Fresh owner authentication plus two 404 responses catches many name/ID drifts;
it does not prove independent source credentials or external writers are safe.
No result from this module by itself authorizes GC.
"""
from __future__ import annotations

import httpx

try:
    from . import reader_gc_credentials as credentials, reader_producer_scope_transition as scope
except ImportError:
    import reader_gc_credentials as credentials
    import reader_producer_scope_transition as scope


TOKENS = {
    'alicetran68/pipeline': ('alicetran68', 'alicetran68/pipeline'),
    'ambrossee768/pdf-pipeline': ('ambrossee768', 'ambrossee768/pdf-pipeline'),
    'ambrossee768/pipeline': ('ambrossee768', 'ambrossee768/pdf-pipeline'),
    'brodievsalas/pipeline': ('brodievsalas', 'brodievsalas/pipeline'),
    'dellamcastillo/pipeline': ('dellamcastillo', 'dellamcastillo/pipeline'),
    'devondunn7/pipeline': ('devondunn7', 'devondunn7/pipeline'),
    'rioholland79/pipeline': ('rioholland79', 'rioholland79/pipeline'),
    'vomebook/pdf-pipeline': ('vomebook', 'vomebook/pdf-pipeline'),
    'vomebook/pipeline': ('vomebook', 'vomebook/pdf-pipeline'),
}


def check(*, get=None, token_for=credentials.token_for):
    """Check each owner identity and exact name/numeric ID with GET only."""
    get = get or httpx.get
    identities = {**scope.EXPECTED, **scope.ADDITIONAL}
    if set(TOKENS) != set(identities):
        raise RuntimeError('retired producer token mapping differs')
    result = []
    for repo, rid in identities.items():
        account, credential_repo = TOKENS[repo]
        if repo.split('/')[0] != account:
            raise RuntimeError('retired producer owner mapping differs')
        token = token_for(credential_repo)
        if not isinstance(token, str) or not token:
            raise RuntimeError('retired producer owner credential unavailable')
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                   'User-Agent': 'reader-retired-producer-name-check'}
        try:
            owner = get('https://api.github.com/user', headers=headers, timeout=30, follow_redirects=False)
            if owner.status_code != 200 or owner.json().get('login', '').lower() != account.lower():
                raise RuntimeError('retired producer owner credential invalid: ' + account)
            named = get('https://api.github.com/repos/' + repo, headers=headers, timeout=30, follow_redirects=False)
            numeric = get('https://api.github.com/repositories/' + str(rid), headers=headers, timeout=30,
                          follow_redirects=False)
        except httpx.TransportError as error:
            raise RuntimeError('retired producer identity read unavailable: ' + repo) from error
        if named.status_code != 404 or numeric.status_code != 404:
            raise RuntimeError('retired producer name or ID not absent: ' + repo)
        result.append({'repository': repo, 'repository_id': rid, 'owner_authenticated': True,
                       'name_status': named.status_code, 'id_status': numeric.status_code})
    return {'kind': 'retired-producer-name-observations', 'repositories': result,
            'source_credentials_revoked': False, 'external_writers_fenced': False, 'gc_authorized': False}
