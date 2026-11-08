"""Atomic parent-checked Space commit for one verified CCRD CW:0 candidate.

The caller must persist a durable intent before apply. This module checks exact
parent paths and bytes, does one parent_commit-guarded HF commit, and never
retries an uncertain response. It cannot update the CCRD bucket pointer.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import ccrd_activation_inspection as activation
    from . import ccrd_candidate_preflight as preflight
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_metadata_patch as metadata
    from . import ccrd_source_override as source
    from . import ccrd_stage_manifest as manifest_stage
    from . import ccrd_stage_objects as staging
    from . import ccrd_staging_plan as planning
except ImportError:
    import ccrd_activation_inspection as activation
    import ccrd_candidate_preflight as preflight
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as metadata
    import ccrd_source_override as source
    import ccrd_stage_manifest as manifest_stage
    import ccrd_stage_objects as staging
    import ccrd_staging_plan as planning


SPACE = 'vomebook/Search'
ACTIVATION = 'data/index-release.json'
SHA = re.compile(r'[0-9a-f]{40}\Z')
MAX_ACTIVATION = 64 * 1024
MAX_MEMBER = metadata.MAX_COMPRESSED


def _get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'pipeline-ccrd-space-activation/1.0',
                                                   'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=90) as response:
        if response.status != 200:
            raise RuntimeError(f'CCRD Space source HTTP {response.status}')
        value = response.read(limit + 1)
    if len(value) > limit:
        raise ValueError('CCRD Space source exceeds limit')
    return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _revision(api, expected: str) -> None:
    if not isinstance(expected, str) or not SHA.fullmatch(expected):
        raise ValueError('invalid expected CCRD Space revision')
    info = api.space_info(SPACE, timeout=20)
    runtime = api.get_space_runtime(SPACE)
    if (info.sha != expected or runtime.stage != 'RUNNING' or not isinstance(runtime.raw, dict)
            or runtime.raw.get('sha') != expected):
        raise ValueError('CCRD Space main or runtime revision differs')


def _url(revision: str, relative: str) -> str:
    return f'https://huggingface.co/spaces/{SPACE}/resolve/{revision}/' + urllib.parse.quote(relative, safe='/')


def _identity(raw: bytes, expected: dict) -> None:
    if not isinstance(raw, bytes) or len(raw) != expected['bytes'] or _sha(raw) != expected['sha256']:
        raise ValueError('CCRD Space parent path differs from the exact parent generation')


def _parent_files(parent: dict, revision: str, *, get=_get) -> dict:
    prefix = f"generations/{parent['generation']}/"
    refs = {item['path'][len(prefix):]: item for item in parent['metadata']}
    selected = {}
    for path in metadata.PATHS:
        item = refs[path]
        raw = get(_url(revision, path), min(item['bytes'], MAX_MEMBER))
        _identity(raw, item)
        selected[path] = {'sha256': item['sha256'], 'bytes': item['bytes']}
    expected = {key: parent['source_overrides'][str(source.CORPUS_PATH)][key] for key in ('bytes', 'sha256')}
    text_path = str(source.CORPUS_PATH)
    old = get(_url(revision, text_path), preflight.MAX_TEXT)
    _identity(old, expected)
    selected[text_path] = expected
    return selected


def _activation(raw: bytes, parent: dict, parent_sha: str) -> dict:
    return activation.decode(raw, parent['generation'], parent_sha)


def _ocr_disabled(api, revision: str, *, get=_get) -> None:
    variables = api.get_space_variables(SPACE)
    if not isinstance(variables, dict):
        raise TypeError('CCRD Space variables are unavailable')
    for key in ('CCRD_OCR_RELEASE_PATH', 'CCRD_OCR_RELEASE_SHA256'):
        item = variables.get(key)
        if item is not None and getattr(item, 'value', None):
            raise ValueError('active CCRD OCR configuration requires a separate compatible activation')
    try:
        get(_url(revision, 'data/ocr-release.json'), MAX_ACTIVATION)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise ValueError('CCRD OCR activation file requires a separate compatible activation')


def plan(parent_raw: bytes, candidate_plan: dict, description: dict, *, api, space_revision: str,
         store, capture, observe, get=_get, current_manifest: bytes, candidate_manifest: bytes) -> dict:
    staging._validate(candidate_plan)
    if planning.make(parent_raw, description) != candidate_plan:
        raise ValueError('CCRD candidate differs from the exact parent and approved description')
    if store.parent_pointer() != parent_raw:
        raise ValueError('CCRD bucket parent changed before Space activation plan')
    checked = manifest_stage.stage_manifest(candidate_plan, approved_description=description, store=store,
                                             capture=capture, observe=observe, apply=False)
    if checked['manifest_present'] is not True or checked['manifest_sha256'] != candidate_plan['manifest_sha256']:
        raise ValueError('CCRD candidate is not fully staged and verified')
    parent = json.loads(parent_raw)
    parent_sha = _sha(parent_raw)
    if current_manifest != parent_raw:
        raise ValueError('CCRD parent immutable manifest differs from current pointer')
    target = lifecycle.canonical(candidate_plan['manifest'])
    if candidate_manifest != target or _sha(target) != candidate_plan['manifest_sha256']:
        raise ValueError('CCRD candidate manifest is not the exact staged immutable manifest')
    _revision(api, space_revision)
    _ocr_disabled(api, space_revision, get=get)
    parent_activation_raw = get(_url(space_revision, ACTIVATION), MAX_ACTIVATION)
    _activation(parent_activation_raw, parent, parent_sha)
    parent_files = _parent_files(parent, space_revision, get=get)
    _ocr_disabled(api, space_revision, get=get)
    _revision(api, space_revision)
    if store.parent_pointer() != parent_raw:
        raise ValueError('CCRD bucket parent changed during Space activation plan')
    candidate_activation = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': lifecycle.BUCKET,
                            'path': f"generations/{candidate_plan['generation']}/manifest.json",
                            'sha256': candidate_plan['manifest_sha256']}
    body = {'version': 1, 'kind': 'ccrd-space-activation-plan', 'space': SPACE,
            'parent_revision': space_revision, 'parent_pointer_sha256': parent_sha,
            'parent_generation': parent['generation'], 'parent_activation_sha256': _sha(parent_activation_raw),
            'parent_activation_hex': parent_activation_raw.hex(),
            'parent_files': parent_files, 'candidate_generation': candidate_plan['generation'],
            'candidate_manifest_sha256': candidate_plan['manifest_sha256'],
            'candidate_description_sha256': description['candidate_identity'], 'activation': candidate_activation,
            'new_text': description['new_text'],
            'candidate_metadata': {path: description['metadata'][path] for path in metadata.PATHS},
            'remote_writes': 0, 'publication_ready': False}
    body['plan_sha256'] = _sha(lifecycle.canonical(body))
    return body


def _local_changes(plan_value: dict, candidate_root: Path, new_text: bytes) -> dict[str, bytes | Path]:
    if preflight._text(new_text) != plan_value['new_text']:
        raise ValueError('CCRD replacement text bytes differ from activation plan')
    files: dict[str, bytes | Path] = {str(source.CORPUS_PATH): new_text,
                                      ACTIVATION: lifecycle.canonical(plan_value['activation'])}
    for name, entry in plan_value['candidate_metadata'].items():
        path = metadata._input(candidate_root, name)
        if metadata.identity(path) != {key: entry[key] for key in ('bytes', 'sha256')}:
            raise ValueError('CCRD candidate metadata changed before Space commit')
        # HF may upload LFS members before the final commit; use the exact
        # bytes verified now rather than a mutable path opened later.
        raw = path.read_bytes()
        if len(raw) != entry['bytes'] or _sha(raw) != entry['sha256']:
            raise ValueError('CCRD candidate metadata changed while preparing commit')
        files[name] = raw
    if set(files) != {*metadata.PATHS, str(source.CORPUS_PATH), ACTIVATION}:
        raise ValueError('CCRD Space candidate files differ from the exact allowlist')
    return files


def apply(plan_value: dict, parent_raw: bytes, candidate_plan: dict, description: dict, *, api, store, capture, observe,
          candidate_root: Path, new_text: bytes, current_manifest: bytes, candidate_manifest: bytes,
          confirm_plan_sha256: str, get=_get) -> dict:
    if not isinstance(plan_value, dict) or plan_value.get('plan_sha256') != confirm_plan_sha256:
        raise ValueError('exact CCRD Space plan confirmation required')
    expected = {key: value for key, value in plan_value.items() if key != 'plan_sha256'}
    if _sha(lifecycle.canonical(expected)) != confirm_plan_sha256:
        raise ValueError('CCRD Space plan identity differs')
    refreshed = plan(parent_raw, candidate_plan, description, api=api, space_revision=plan_value['parent_revision'],
                     store=store, capture=capture, observe=observe, get=get,
                     current_manifest=current_manifest, candidate_manifest=candidate_manifest)
    if refreshed != plan_value:
        raise ValueError('CCRD Space parent or candidate changed before commit')
    changes = _local_changes(plan_value, candidate_root, new_text)
    if store.parent_pointer() != parent_raw:
        raise ValueError('CCRD bucket parent changed while preparing Space commit')
    manifest_stage._source_check(candidate_plan, capture())
    manifest_stage._consumer_check(json.loads(parent_raw), observe())
    _ocr_disabled(api, plan_value['parent_revision'], get=get)
    _revision(api, plan_value['parent_revision'])
    from huggingface_hub import CommitOperationAdd
    operations = [CommitOperationAdd(path_in_repo=path, path_or_fileobj=value) for path, value in sorted(changes.items())]
    # Exactly one parent-guarded commit. Any exception may hide a committed
    # result; the caller must inspect the Space before any recovery or retry.
    commit = api.create_commit(repo_id=SPACE, repo_type='space', parent_commit=plan_value['parent_revision'],
                               commit_message='Update verified CCRD CW source and index activation', operations=operations)
    revision = getattr(commit, 'oid', None)
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise RuntimeError('CCRD Space commit response lacks an exact revision; inspect before recovery')
    if api.space_info(SPACE, timeout=20).sha != revision:
        raise RuntimeError('CCRD Space commit readback differs; inspect before recovery')
    return {'version': 1, 'kind': 'ccrd-space-activation-commit', 'revision': revision,
            'generation': candidate_plan['generation'], 'parent_revision': plan_value['parent_revision'],
            'files_written': sorted(changes), 'runtime_verified': False, 'consumer_verified': False,
            'pointer_written': False, 'external_writers_verified': False}
