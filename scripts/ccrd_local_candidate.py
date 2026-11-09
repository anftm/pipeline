"""Build one checked CCRD CW:0 candidate locally, without remote writes.

All inputs must already be ordinary local files. Exact parent, 17 metadata
members, CW database and pinned source evidence are rechecked. The outputs are
not a publication, a consumer receipt or authorization to mutate any site.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

try:
    from . import ccrd_candidate_assembly as assembly
    from . import ccrd_cw_candidate as cw
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_metadata_patch as metadata
    from . import ccrd_staging_plan as planning
except ImportError:
    import ccrd_candidate_assembly as assembly
    import ccrd_cw_candidate as cw
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as metadata
    import ccrd_staging_plan as planning


def _new_output(output: Path, inputs: tuple[Path, ...]) -> None:
    if output.exists() or output.is_symlink() or not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError('CCRD local candidate output must be a new path in an existing ordinary directory')
    target = output.resolve()
    for source in inputs:
        root = source.resolve()
        if target == root or root in target.parents or target in root.parents:
            raise ValueError('CCRD local candidate output must be separate from all input paths')


def build(pointer: bytes, old: bytes, new: bytes, *, consumer: dict, source_observation: dict,
          parent_metadata: Path, parent_cw: Path, output: Path) -> dict:
    """Build and fully compare CW SQLite and metadata in a new local directory."""
    _new_output(output, (parent_metadata, parent_cw))
    if not isinstance(pointer, bytes) or not 0 < len(pointer) <= assembly.MAX_PLAN:
        raise ValueError('invalid CCRD parent pointer bytes')
    parent = json.loads(pointer)
    lifecycle.references(parent, 'current.json')
    pointer_sha = lifecycle.digest(pointer)
    if not isinstance(source_observation, dict):
        raise TypeError('CCRD pinned source observation must be an object')
    # All source and consumer identity checks precede creating any output.
    assembly.preflight.plan(pointer, old, new, consumer, source_url=source_observation.get('url'),
                            source_revision=source_observation.get('revision'), source_observation=source_observation)
    refs = assembly._metadata_parent(parent)
    if not parent_metadata.is_dir() or parent_metadata.is_symlink():
        raise ValueError('CCRD parent metadata directory is missing or symlinked')
    expected_db = {key: parent['databases']['CW'][key] for key in ('bytes', 'sha256')}
    if cw.file_identity(parent_cw, cw.MAX_DB_BYTES) != expected_db:
        raise ValueError('CCRD parent CW database bytes differ from manifest')
    expected_meta = {name: {key: refs[name][key] for key in ('bytes', 'sha256')} for name in metadata.PATHS}
    output.mkdir(mode=0o700)
    try:
        old_path, new_path = output / 'old-source.txt', output / 'new-source.txt'
        with old_path.open('xb') as stream: stream.write(old)
        with new_path.open('xb') as stream: stream.write(new)
        cw_report = cw.build(parent_cw, old_path, new_path, output / 'cw',
                             source_identity=expected_db, old_identity=assembly.preflight._text(old),
                             new_identity=assembly.preflight._text(new),
                             expected_documents=parent['databases']['CW']['documents'])
        meta_report = metadata.build(parent_metadata, output / 'metadata', expected=expected_meta,
                                     old_text=old.decode('utf-8'), new_text=new.decode('utf-8'), manifest=parent)
        description = assembly.assemble(pointer, old, new, consumer, source_observation,
                                        approved_pointer_sha256=pointer_sha, parent_metadata=parent_metadata,
                                        candidate_metadata=output / 'metadata', parent_cw=parent_cw,
                                        candidate_cw=output / 'cw/CW.sqlite3')
        staging = planning.make(pointer, description)
        for name, value in (('cw-report.json', cw_report), ('metadata-report.json', meta_report),
                            ('description.json', description), ('staging-plan.json', staging)):
            with (output / name).open('xb') as stream:
                stream.write(lifecycle.canonical(value))
        return {'version': 1, 'kind': 'ccrd-cw0-local-build-result', 'parent_generation': parent['generation'],
                'generation': staging['generation'], 'candidate_description_sha256': description['candidate_identity'],
                'manifest_sha256': staging['manifest_sha256'], 'metadata_members': len(description['metadata']),
                'archive_members': len(description['archives']), 'remote_writes': 0, 'pointer_written': False,
                'consumer_switched': False, 'publication_ready': False, 'external_writers_verified': False}
    except BaseException:
        # This directory was created exclusively by this call; do not remove
        # any caller-owned directory or input.
        shutil.rmtree(output)
        raise


def _bounded(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError('CCRD local candidate input is missing, symlinked or oversized')
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise ValueError('CCRD local candidate input grew during read')
    return raw


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('pointer', 'old', 'new', 'consumer', 'source-observation', 'parent-metadata', 'parent-cw', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args(argv)
    _new_output(args.output, (args.pointer, args.old, args.new, args.consumer, args.source_observation,
                              args.parent_metadata, args.parent_cw))
    result = build(_bounded(args.pointer, assembly.MAX_PLAN), _bounded(args.old, cw.MAX_TEXT_BYTES),
                   _bounded(args.new, cw.MAX_TEXT_BYTES), consumer=json.loads(_bounded(args.consumer, 256 * 1024)),
                   source_observation=json.loads(_bounded(args.source_observation, 256 * 1024)),
                   parent_metadata=args.parent_metadata, parent_cw=args.parent_cw, output=args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
