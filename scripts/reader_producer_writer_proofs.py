"""Fail-closed model for source-side retirement of worker write credentials.

GitHub repository deletion removes repository-held secrets but cannot prove
that their Hugging Face or GitHub values were independently revoked. Secret
names, 404s, another token's scopes and caller-provided flags are not proof.
No token, token prefix, digest, or credential value belongs in this evidence.
This offline model deliberately has no live provider adapter or activation.
"""
from __future__ import annotations

import hashlib
import json

try:
    from . import reader_producer_scope_transition as scope
except ImportError:
    import reader_producer_scope_transition as scope


REQUIRED_BY_KIND = {
    'deleted_worker': frozenset({'HF_TOKEN', 'HF_INPUT_TOKEN', 'PDF_PUBLISH_TOKEN'}),
    'additional_ambrossee': frozenset({'HF_TOKEN', 'HF_INPUT_TOKEN', 'HF_S3_ACCESS_KEY_ID',
                                       'HF_S3_SECRET_ACCESS_KEY', 'PAGES_REPO', 'PAGES_TOKEN',
                                       'PDF_PUBLISH_TOKEN'}),
    'additional_vomebook': frozenset({'ARCHIVE_HF_TOKEN', 'HF_INPUT_TOKEN', 'HF_TOKEN',
                                      'PDF_PUBLISH_TOKEN', 'PIPELINE_TOKEN'}),
}
ALL_REPOSITORIES = {**scope.EXPECTED, **scope.ADDITIONAL}


def requirements():
    result = {repo: sorted(REQUIRED_BY_KIND['deleted_worker']) for repo in scope.EXPECTED}
    result['ambrossee768/pipeline'] = sorted(REQUIRED_BY_KIND['additional_ambrossee'])
    result['vomebook/pipeline'] = sorted(REQUIRED_BY_KIND['additional_vomebook'])
    return result


def build_inventory(records):
    """Bind all observed repository IDs and secret *names*, never their values.

    This is an inventory for further work, not a revocation or isolation proof.
    Every listed name is marked unknown even when another token can be queried.
    """
    expected = requirements()
    if not isinstance(records, dict) or set(records) != set(ALL_REPOSITORIES):
        raise ValueError('source-writer inventory repository set differs')
    rows = []
    for repo in sorted(ALL_REPOSITORIES):
        entry = records[repo]
        if (not isinstance(entry, dict) or set(entry) != {'repository_id', 'secret_names'}
                or type(entry.get('repository_id')) is not int or entry['repository_id'] != ALL_REPOSITORIES[repo]
                or not isinstance(entry.get('secret_names'), list)
                or any(not isinstance(name, str) for name in entry['secret_names'])
                or sorted(entry['secret_names']) != expected[repo]
                or len(set(entry['secret_names'])) != len(entry['secret_names'])):
            raise ValueError('source-writer secret inventory differs: ' + repo)
        rows.append({'repository': repo, 'repository_id': entry['repository_id'],
                     'secrets': [{'name': name, 'source_status': 'unknown'} for name in expected[repo]]})
    return {'version': 1, 'kind': 'retired-worker-source-writer-inventory', 'repositories': rows,
            'external_writer_isolation_verified': False, 'gc_authorized': False}


def inventory_digest(value):
    return hashlib.sha256((json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()).hexdigest()


def require_source_isolation(_inventory, _evidence):
    """Provider-side identity/revocation verification is not implemented."""
    raise RuntimeError('source-side writer credential isolation has not been verified')
