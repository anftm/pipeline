"""Build and stage a changed CCRD CW:0 source from verified live inputs.

Only immutable candidate generation objects and its manifest may be created.
This does not activate Space, change current.json, or fence external writers.
An unchanged source exits without a build or any remote writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from . import ccrd_candidate_assembly as assembly
    from . import ccrd_consumer_inspection as consumer
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_local_candidate as local
    from . import ccrd_source_snapshot as snapshot
    from . import ccrd_space_activation as space
    from . import ccrd_stage_manifest as manifests
    from . import ccrd_stage_objects as objects
    from . import ccrd_transaction as tx
    from . import ccrd_transaction_s3 as journal_s3
except ImportError:
    import ccrd_candidate_assembly as assembly
    import ccrd_consumer_inspection as consumer
    import ccrd_lifecycle as lifecycle
    import ccrd_local_candidate as local
    import ccrd_source_snapshot as snapshot
    import ccrd_space_activation as space
    import ccrd_stage_manifest as manifests
    import ccrd_stage_objects as objects
    import ccrd_transaction as tx
    import ccrd_transaction_s3 as journal_s3


MAX_LOCAL_BYTES = 8 * 1024 * 1024 * 1024
READ_ROOT = 'https://huggingface.co/buckets/vomebook/ccrd-index/resolve/'


def _read_pointer() -> bytes:
    request = Request(READ_ROOT + 'current.json', headers={'User-Agent': 'pipeline-ccrd-stage/1.0',
                                                          'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
    with urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError('CCRD current pointer read failed')
        raw = response.read(assembly.MAX_PLAN + 1)
    if not 0 < len(raw) <= assembly.MAX_PLAN:
        raise ValueError('CCRD current pointer is empty or oversized')
    lifecycle.references(json.loads(raw), 'current.json')
    return raw


def _download(key: str, expected: dict, path: Path) -> None:
    lifecycle.safe_path(key)
    size, digest = expected.get('bytes'), expected.get('sha256')
    if type(size) is not int or not 0 < size <= objects.MAX_BYTES:
        raise ValueError('CCRD input size is outside the download limit')
    lifecycle.sha(digest)
    if path.exists() or path.is_symlink():
        raise ValueError('CCRD input download path already exists')
    request = Request(READ_ROOT + quote(key, safe='/'), headers={'User-Agent': 'pipeline-ccrd-stage/1.0',
                                                               'Accept-Encoding': 'identity'})
    path.parent.mkdir(parents=True, exist_ok=True)
    total, hashed = 0, hashlib.sha256()
    with urlopen(request, timeout=120) as response, path.open('xb') as output:
        if response.status != 200:
            raise RuntimeError('CCRD object download failed')
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > size:
                raise ValueError('CCRD object exceeds expected size')
            hashed.update(chunk); output.write(chunk)
    if total != size or hashed.hexdigest() != digest:
        raise ValueError('CCRD downloaded object differs from manifest: ' + key)


def _source(token: str) -> tuple[bytes, dict]:
    return snapshot.capture(token)


def run(work: Path, *, apply: bool, token: str, read_pointer=_read_pointer, capture=_source,
        download=_download, observe=consumer.observe, get=consumer.get_bytes,
        store_factory=objects.S3Store, journal_factory=journal_s3.S3Store) -> dict:
    if not isinstance(work, Path) or work.exists() or work.is_symlink() or not work.parent.is_dir():
        raise ValueError('CCRD work path must be a new directory')
    parent_raw = read_pointer(); parent = json.loads(parent_raw)
    lifecycle.references(parent, 'current.json')
    refs = assembly._metadata_parent(parent)
    expected_text = {key: parent['source_overrides'][str(space.source.CORPUS_PATH)][key]
                     for key in ('bytes', 'sha256')}
    upstream, source_info = capture(token)
    actual = {'bytes': len(upstream), 'sha256': hashlib.sha256(upstream).hexdigest()}
    if any(source_info.get(key) != value for key, value in actual.items()) or source_info.get('source_stable') is not True:
        raise ValueError('CCRD live source capture identity differs')
    prior_consumer = observe(parent['generation'], expected_text)
    if read_pointer() != parent_raw:
        raise ValueError('CCRD parent changed during source inspection')
    if actual == expected_text:
        return {'kind': 'ccrd-auto-stage', 'status': 'unchanged', 'parent': parent['generation'],
                'remote_writes': 0, 'pointer_written': False, 'consumer_switched': False,
                'publication_ready': False, 'external_writers_verified': False}
    journal_store = journal_factory() if apply else None
    journal = tx.load(journal_store) if journal_store is not None else None
    if journal is not None:
        tx.audit(journal_store, journal)
        if journal.record['state'] not in {'complete', 'rolled-back'}:
            raise ValueError('CCRD publication journal has unfinished work')
    old = get(consumer.BASE + '/api/download/CW:0', consumer.MAX_TEXT_BYTES)
    if {'bytes': len(old), 'sha256': hashlib.sha256(old).hexdigest()} != expected_text:
        raise ValueError('CCRD parent consumer text differs before building')
    if observe(parent['generation'], expected_text) != prior_consumer or read_pointer() != parent_raw:
        raise ValueError('CCRD parent consumer or pointer changed before build')
    entries = [*refs.values(), parent['databases']['CW'], *parent['archives']]
    if sum(entry['bytes'] for entry in entries) > MAX_LOCAL_BYTES:
        raise ValueError('CCRD local input download budget exceeded')
    work.mkdir(mode=0o700)
    metadata_root = work / 'parent'; metadata_root.mkdir()
    prefix = f"generations/{parent['generation']}/"
    for entry in [*refs.values(), *parent['archives']]:
        key = entry['path']
        if not key.startswith(prefix):
            raise ValueError('CCRD input is outside exact parent generation')
        relative = key[len(prefix):]; lifecycle.safe_path(relative)
        download(key, entry, metadata_root / relative)
    cw_path = work / 'parent-CW.sqlite3'
    download(parent['databases']['CW']['path'], parent['databases']['CW'], cw_path)
    result = local.build(parent_raw, old, upstream, consumer=prior_consumer, source_observation=source_info,
                         parent_metadata=metadata_root, parent_cw=cw_path, output=work / 'candidate')
    description = json.loads((work / 'candidate/description.json').read_bytes())
    plan = json.loads((work / 'candidate/staging-plan.json').read_bytes())
    current_source, later_source = capture(token)
    if current_source != upstream or later_source != source_info or read_pointer() != parent_raw:
        raise ValueError('CCRD source or parent changed during local build')
    if observe(parent['generation'], expected_text) != prior_consumer:
        raise ValueError('CCRD parent consumer changed during local build')
    with (work / 'report.json').open('x', encoding='utf-8') as output:
        json.dump({'kind': 'ccrd-auto-stage-build', 'parent': parent['generation'], 'generation': plan['generation'],
                   'manifest_sha256': plan['manifest_sha256'], 'publication_ready': False}, output, sort_keys=True)
        output.write('\n')
    if not apply:
        return {**result, 'kind': 'ccrd-auto-stage', 'status': 'built-local-only'}
    journal_store = journal_factory(); current = tx.load(journal_store)
    if current is not None:
        tx.audit(journal_store, current)
        if current.record['state'] not in {'complete', 'rolled-back'}:
            raise ValueError('CCRD publication journal became active before staging')
    store = store_factory(generation=plan['generation'])
    if store.parent_pointer() != parent_raw:
        raise ValueError('CCRD S3 parent differs from verified parent')
    staged = objects.stage(plan, approved_description=description, parent_root=metadata_root,
                           candidate_root=work / 'candidate/metadata', candidate_cw=work / 'candidate/cw/CW.sqlite3',
                           store=store)
    source_capture = lambda: capture(token)[1]
    consumer_capture = lambda: observe(parent['generation'], expected_text)
    staged_manifest = manifests.stage_manifest(plan, approved_description=description, store=store,
                                                capture=source_capture, observe=consumer_capture, apply=True)
    return {'kind': 'ccrd-auto-stage', 'status': 'staged', 'parent': parent['generation'],
            'generation': plan['generation'], 'manifest_sha256': plan['manifest_sha256'],
            'objects_verified': staged['objects_verified'], 'objects_created': staged['objects_created'],
            'manifest_written': staged_manifest['manifest_written'], 'pointer_written': False,
            'consumer_switched': False, 'publication_ready': False, 'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--stage', action='store_true')
    args = parser.parse_args(argv)
    if (args.report.exists() or args.report.is_symlink() or args.report.resolve() == args.work.resolve()
            or args.work.resolve() in args.report.resolve().parents):
        raise ValueError('CCRD report must be a new path outside the work directory')
    result = run(args.work, apply=args.stage, token=os.environ.get('GITHUB_TOKEN', ''))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, sort_keys=True, ensure_ascii=False); stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
