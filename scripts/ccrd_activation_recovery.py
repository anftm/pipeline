"""Inspect an uncertain CCRD Space activation without retrying any writes.

Recovery only recognizes the exact parent or a single direct child containing
all six planned byte identities. A later foreign commit, unready runtime,
partial deployment, missing source bytes or ambiguous response fails closed.
This does not fence concurrent writers or itself roll anything back.
"""
from __future__ import annotations

import hashlib
import re
import subprocess

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_metadata_patch as metadata
    from . import ccrd_space_activation as space
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as metadata
    import ccrd_space_activation as space


REVISION = re.compile(r'[0-9a-f]{40}\Z')


def _plan(value: dict) -> dict:
    if not isinstance(value, dict) or value.get('kind') != 'ccrd-space-activation-plan':
        raise ValueError('invalid CCRD activation intent')
    digest = value.get('plan_sha256')
    lifecycle.sha(digest)
    body = {key: item for key, item in value.items() if key != 'plan_sha256'}
    if hashlib.sha256(lifecycle.canonical(body)).hexdigest() != digest:
        raise ValueError('CCRD activation intent hash differs')
    if value.get('space') != space.SPACE or not isinstance(value.get('parent_revision'), str) \
            or not REVISION.fullmatch(value['parent_revision']):
        raise ValueError('invalid CCRD activation parent identity')
    original_hex = value.get('parent_activation_hex')
    if not isinstance(original_hex, str) or len(original_hex) > 2 * space.MAX_ACTIVATION:
        raise ValueError('missing bounded original activation bytes')
    try:
        original = bytes.fromhex(original_hex)
    except ValueError as error:
        raise ValueError('invalid original activation bytes') from error
    if hashlib.sha256(original).hexdigest() != value.get('parent_activation_sha256'):
        raise ValueError('CCRD original activation identity differs')
    return value


def _identity(raw: bytes, expected: dict, path: str) -> None:
    if (not isinstance(raw, bytes) or len(raw) != expected['bytes']
            or hashlib.sha256(raw).hexdigest() != expected['sha256']):
        raise ValueError('CCRD recovery path differs: ' + path)


def _parent_map(value: dict) -> dict:
    entries = value.get('parent_files')
    expected = {*metadata.PATHS, str(space.source.CORPUS_PATH)}
    if not isinstance(entries, dict) or set(entries) != expected:
        raise ValueError('CCRD recovery parent file inventory differs')
    result = dict(entries)
    raw = bytes.fromhex(value['parent_activation_hex'])
    result[space.ACTIVATION] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    return result


def _candidate_map(value: dict) -> dict:
    entries = value.get('candidate_metadata')
    if not isinstance(entries, dict) or set(entries) != set(metadata.PATHS):
        raise ValueError('CCRD recovery candidate file inventory differs')
    result = {name: {key: entry[key] for key in ('bytes', 'sha256')} for name, entry in entries.items()}
    result[str(space.source.CORPUS_PATH)] = value['new_text']
    raw = lifecycle.canonical(value['activation'])
    result[space.ACTIVATION] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    return result


def direct_child(parent: str, child: str) -> bool:
    """Read the two named Git commits and require one exact parent."""
    if not REVISION.fullmatch(parent) or not REVISION.fullmatch(child):
        raise ValueError('invalid CCRD commit identity')
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ccrd-recovery-parent-') as directory:
        subprocess.run(['git', 'init', '--bare', directory], check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(['git', '--git-dir', directory, 'fetch', '--no-tags', '--depth=2',
                        'https://huggingface.co/spaces/vomebook/Search', child],
                       check=True, capture_output=True, text=True, timeout=120)
        result = subprocess.run(['git', '--git-dir', directory, 'rev-list', '--parents', '-n', '1', child],
                                check=True, capture_output=True, text=True, timeout=30)
        return result.stdout.strip().split() == [child, parent]


def observe(plan_value: dict, *, api, get=space._get, child_of=direct_child) -> dict:
    """Observe exact current revision/files only; perform no mutations."""
    value = _plan(plan_value)
    current = getattr(api.space_info(space.SPACE, timeout=20), 'sha', None)
    if not isinstance(current, str) or not REVISION.fullmatch(current):
        raise ValueError('CCRD recovery cannot establish current Space revision')
    parent = value['parent_revision']
    if current == parent:
        status = 'parent'
        expected = _parent_map(value)
    else:
        if child_of(parent, current) is not True:
            raise ValueError('CCRD current revision is not the exact direct child of the intended parent')
        status = 'candidate'
        expected = _candidate_map(value)
    for path, identity in expected.items():
        limit = identity['bytes']
        if type(limit) is not int or not 0 < limit <= space.MAX_MEMBER:
            raise ValueError('CCRD recovery member length is invalid')
        _identity(get(space._url(current, path), limit), identity, path)
    if api.space_info(space.SPACE, timeout=20).sha != current:
        raise RuntimeError('CCRD Space changed during recovery inspection')
    runtime = api.get_space_runtime(space.SPACE)
    if runtime.stage != 'RUNNING' or not isinstance(runtime.raw, dict) or runtime.raw.get('sha') != current:
        raise ValueError('CCRD recovery runtime does not match the exact observed revision')
    return {'version': 1, 'kind': 'ccrd-space-recovery-observation', 'status': status,
            'revision': current, 'parent_revision': parent, 'plan_sha256': value['plan_sha256'],
            'files_verified': len(expected), 'remote_writes': 0,
            'external_writers_verified': False, 'consumer_content_verified': False}
