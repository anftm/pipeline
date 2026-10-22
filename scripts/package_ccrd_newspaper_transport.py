#!/usr/bin/env python3
"""Export bounded public Release assets locally; never upload or activate them."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:
    from . import ccrd_newspaper_transport as transport
    from . import ccrd_lifecycle as lifecycle
except ImportError:
    import ccrd_newspaper_transport as transport
    import ccrd_lifecycle as lifecycle


def package(plan, papers, runtime, previous_runtime_sha256, release_url, output, *, oracle_process_receipt=None):
    if (plan.get('sample_only') is not False or plan.get('complete_newspaper_audits') is not True
            or plan.get('kind') != 'ccrd-four-source-local-publication-plan'):
        raise ValueError('cannot transport a sample or incomplete newspaper plan')
    papers, runtime = papers.resolve(), runtime.resolve()
    evidence_paths = {name: papers / name for name in transport.EVIDENCE_FILES}
    if oracle_process_receipt is not None:
        evidence_paths['oracle-process-receipt.json'] = oracle_process_receipt.resolve()
    evidence = {name: json.loads(path.read_text()) for name, path in evidence_paths.items()}
    counts = {source: plan['documents'][source] for source in ['RMRB', 'PLA']}
    oracle = evidence['oracle-receipt.json']
    if (evidence['oracle-process-receipt.json'].get('exit_code') != 0
            or evidence['oracle-process-receipt.json'].get('runtime_sha256_unchanged') is not True
            or oracle.get('queries') != 22
            or {source: result['documents'] for source, result in oracle.get('sources', {}).items()} != counts):
        raise ValueError('full independent original-TXT oracle must pass before transport')
    app_sha = transport.identity(runtime / 'app.py')['sha256']
    if (oracle.get('runtime_sha256') != app_sha
            or evidence['oracle-process-receipt.json'].get('runtime_sha256') != app_sha
            or evidence['full-runtime-acceptance-receipt.json'].get('runtime_sha256') != app_sha
            or evidence['full-package-acceptance-receipt.json'].get('metadata', {}).get('source_counts') != plan['documents']):
        raise ValueError('transport evidence does not describe the accepted full runtime and package')
    if any(value.get('remote_writes') != 0 for value in evidence.values()):
        raise ValueError('expected local-only acceptance evidence')
    output.mkdir(parents=True, exist_ok=False)
    prefix = transport.release_prefix(release_url)
    files = []
    selected = [(Path(ref['local_path']), ref['path'], ref) for ref in plan['upload_files']]
    selected += [(runtime / name, 'runtime/' + name, None) for name in sorted(transport.RUNTIME_FILES)]
    selected += [(evidence_paths[name], 'evidence/' + name, None) for name in sorted(transport.EVIDENCE_FILES)]
    for local, path, expected in selected:
        details = transport.identity(local)
        if expected and any(details[key] != expected[key] for key in details):
            raise ValueError('accepted candidate changed before transport')
        parts = []
        with local.open('rb') as stream:
            while True:
                # Stream to one reusable temporary part; do not hold GiB blobs
                # in memory or retain a second complete corpus in RAM.
                temporary = output / 'next-part.tmp'
                digest, size = hashlib.sha256(), 0
                with temporary.open('wb') as part:
                    while size < transport.MAX_PART:
                        block = stream.read(min(1024 * 1024, transport.MAX_PART - size))
                        if not block:
                            break
                        part.write(block)
                        digest.update(block)
                        size += len(block)
                if not size:
                    temporary.unlink()
                    break
                sha = digest.hexdigest()
                target = output / f'part-{sha}.bin'
                if target.exists():
                    if transport.identity(target) != {'sha256': sha, 'bytes': size}:
                        raise ValueError('conflicting content-addressed transport part')
                    temporary.unlink()
                else:
                    temporary.replace(target)
                parts.append({'url': prefix + target.name, 'sha256': sha, 'bytes': size})
        files.append({'path': path, **details, 'parts': parts})
    value = {'version': 1, 'kind': transport.KIND, 'sample_only': False,
             'manifest': plan['manifest'], 'builder_sha256': plan['manifest']['builder']['sha256'],
             'previous_runtime_sha256': previous_runtime_sha256, 'files': files}
    transport.validate(value, release_url)
    target = output / release_url.rsplit('/', 1)[1]
    target.write_bytes(lifecycle.canonical(value))
    result = {'descriptor': str(target), **transport.identity(target), 'files': len(files),
              'release_url': release_url, 'remote_writes': 0, 'publication_ready': False,
              'selected_oracle_process_receipt': str(evidence_paths['oracle-process-receipt.json'])}
    (output / 'local-transport-receipt.json').write_bytes(lifecycle.canonical(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--papers', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--previous-runtime-hashes', type=Path, required=True)
    parser.add_argument('--release-url', required=True)
    parser.add_argument('--oracle-process-receipt', type=Path, required=True,
                        help='Exact successful process receipt; failed/interrupted attempts remain untouched')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = package(json.loads(args.plan.read_text()), args.papers, args.runtime,
        json.loads(args.previous_runtime_hashes.read_text()), args.release_url, args.output,
        oracle_process_receipt=args.oracle_process_receipt)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
