"""Activate one previously staged CCRD candidate using durable recovery state.

This command is deliberately manual. It checks exact staged objects, the live
source and Space before recording an intent, then performs one parent-guarded
commit. Unknown outcomes are left in the journal and never replayed here.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

try:
    from . import ccrd_auto_stage as stage
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_local_candidate as local
    from . import ccrd_pointer_transition as pointer
    from . import ccrd_publication_coordinator as coordinator
    from . import ccrd_space_activation as space
    from . import ccrd_stage_objects as objects
    from . import ccrd_transaction_s3 as journal_s3
except ImportError:
    import ccrd_auto_stage as stage
    import ccrd_lifecycle as lifecycle
    import ccrd_local_candidate as local
    import ccrd_pointer_transition as pointer
    import ccrd_publication_coordinator as coordinator
    import ccrd_space_activation as space
    import ccrd_stage_objects as objects
    import ccrd_transaction_s3 as journal_s3


MAX_BUNDLE = 2 * 1024 * 1024 * 1024


def _path(root: Path, relative: str, limit: int) -> bytes:
    path = root / relative
    if root.is_symlink() or any((root / '/'.join(relative.split('/')[:i])).is_symlink()
                                for i in range(1, len(relative.split('/')) + 1)):
        raise ValueError('CCRD staged artifact contains a symlink')
    return local._bounded(path, limit)


def _bundle(bundle: Path) -> tuple[bytes, dict, dict, bytes]:
    """Read the exact flattened workflow artifact layout."""
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError('CCRD candidate bundle must be an ordinary directory')
    allowed = {'parent-pointer.json', 'stage-result.json', 'description.json', 'staging-plan.json', 'new-source.txt'}
    allowed_dirs = {parent.as_posix() for name in allowed for parent in Path(name).parents if parent.as_posix() != '.'}
    actual, total, count = set(), 0, 0
    for item in bundle.rglob('*'):
        count += 1
        if count > len(allowed) + len(allowed_dirs):
            raise ValueError('CCRD candidate bundle has too many entries')
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise ValueError('CCRD staged artifact contains a symlink or special file')
        relative = item.relative_to(bundle).as_posix()
        if item.is_dir() and relative not in allowed_dirs:
            raise ValueError('CCRD candidate bundle has an unexpected directory')
        if item.is_file():
            actual.add(relative); total += item.stat().st_size
            if total > MAX_BUNDLE:
                raise ValueError('CCRD candidate bundle exceeds the size limit')
    if actual != allowed:
        raise ValueError('CCRD candidate bundle does not contain the exact approved files')
    parent_raw = _path(bundle, 'parent-pointer.json', local.assembly.MAX_PLAN)
    description = json.loads(_path(bundle, 'description.json', local.assembly.MAX_PLAN))
    plan = json.loads(_path(bundle, 'staging-plan.json', local.assembly.MAX_PLAN))
    receipt = json.loads(_path(bundle, 'stage-result.json', 64 * 1024))
    new_text = _path(bundle, 'new-source.txt', local.cw.MAX_TEXT_BYTES)
    if (not isinstance(description, dict) or not isinstance(plan, dict) or not isinstance(receipt, dict)
            or receipt != {'generation': plan.get('generation'), 'manifest_sha256': plan.get('manifest_sha256'),
                           'candidate_description_sha256': description.get('candidate_identity')}):
        raise ValueError('CCRD stage artifact identity differs')
    if stage.planning.make(parent_raw, description) != plan:
        raise ValueError('CCRD staged artifact does not reproduce the exact generation plan')
    if local.assembly.preflight._text(new_text) != plan['identity']['new_text']:
        raise ValueError('CCRD staged source does not match generation plan')
    return parent_raw, description, plan, new_text


def _materialize_metadata(root: Path, plan: dict, description: dict, *, download=stage._download) -> None:
    if root.exists() or root.is_symlink() or not root.parent.is_dir():
        raise ValueError('CCRD metadata work directory must be a new path')
    root.mkdir(mode=0o700)
    prefix = f"generations/{plan['generation']}/"
    for relative in space.metadata.PATHS:
        expected = description['metadata'][relative]
        key = prefix + relative
        download(key, expected, root / relative)
        if space.metadata.identity(root / relative) != {key: expected[key] for key in ('bytes', 'sha256')}:
            raise ValueError('CCRD staged candidate metadata readback differs')


def start(bundle: Path, *, token: str, api, journal, pointer_store, store_factory=objects.S3Store,
          capture=stage._source, get=space._get, download=stage._download, metadata_root: Path) -> dict:
    """Check the actual candidate and request exactly one Space activation."""
    parent_raw, description, plan, new_text = _bundle(bundle)
    existing = coordinator.tx.load(journal)
    if existing is not None:
        coordinator.tx.audit(journal, existing)
        if existing.record['state'] not in {'complete', 'rolled-back'}:
            raise ValueError('CCRD journal has unfinished work; reconcile instead of starting again')
    parent = json.loads(parent_raw); lifecycle.references(parent, 'current.json')
    target = lifecycle.canonical(plan['manifest'])
    if stage.assembly.preflight._text(new_text) != plan['identity']['new_text']:
        raise ValueError('CCRD artifact source differs from candidate')
    active, _etag = pointer_store.read_for_update()
    if active != parent_raw:
        raise ValueError('CCRD live pointer differs from staged parent')
    store = store_factory(generation=plan['generation'])
    immutable = store.read_manifest()
    if immutable != target:
        raise ValueError('CCRD staged immutable manifest is missing or changed')
    info = api.space_info(space.SPACE, timeout=20)
    revision = getattr(info, 'sha', None)
    source_capture = lambda: capture(token)[1]
    text = parent['source_overrides'][str(space.source.CORPUS_PATH)]
    expected = {key: text[key] for key in ('bytes', 'sha256')}
    consume = lambda: stage.consumer.observe(parent['generation'], expected)
    parent_manifest = stage._read_pointer()
    # Fetch the four exact immutable members before claiming any write.
    _materialize_metadata(metadata_root, plan, description, download=download)
    provisional = {'new_text': description['new_text'],
                   'activation': {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': lifecycle.BUCKET,
                                  'path': f"generations/{plan['generation']}/manifest.json",
                                  'sha256': plan['manifest_sha256']},
                   'candidate_metadata': {path: description['metadata'][path] for path in space.metadata.PATHS}}
    space._local_changes(provisional, metadata_root, new_text)
    plan_space = space.plan(parent_raw, plan, description, api=api, space_revision=revision, store=store,
                            capture=source_capture, observe=consume, get=get,
                            current_manifest=parent_manifest, candidate_manifest=immutable)
    transaction = secrets.token_hex(16)
    print(json.dumps({'kind': 'ccrd-activation-intent-start', 'transaction': transaction,
                      'candidate': plan['generation'], 'external_writers_verified': False}, sort_keys=True), flush=True)
    current = coordinator.prepare(journal, parent_pointer=parent_raw, candidate_plan=plan, description=description,
                                  activation_plan=plan_space, pointer_store=pointer_store, transaction=transaction)
    print(json.dumps({'kind': 'ccrd-activation-intent', 'transaction': current.record['transaction'],
                      'candidate': plan['generation'], 'pointer_written': False,
                      'external_writers_verified': False}, sort_keys=True), flush=True)
    coordinator.start_space(journal)
    result = coordinator.apply_space_once(journal, candidate_plan=plan, description=description, api=api,
                                          staging_store=store, capture=source_capture, observe=consume,
                                          candidate_root=metadata_root, new_text=new_text,
                                          current_manifest=parent_manifest, candidate_manifest=immutable, get=get)
    return {'kind': 'ccrd-activation-request', 'transaction': current.record['transaction'],
            'generation': plan['generation'], 'space_revision': result['revision'],
            'consumer_verified': False, 'pointer_written': False, 'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--metadata-root', required=True, type=Path)
    parser.add_argument('--known-writers-coordinated', action='store_true')
    args = parser.parse_args(argv)
    if not args.known_writers_coordinated:
        raise ValueError('actual CCRD and Space writers must be coordinated')
    from huggingface_hub import HfApi
    result = start(args.bundle, token=os.environ.get('GITHUB_TOKEN', ''), api=HfApi(token=os.environ.get('HF_TOKEN')),
                   journal=journal_s3.S3Store(), pointer_store=pointer.S3Pointer(), metadata_root=args.metadata_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
