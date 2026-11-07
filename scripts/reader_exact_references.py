#!/usr/bin/env python3
"""Explain exact Reader references without a full bucket inventory or deletion.

The report is deliberately incomplete: nested manifests, unknown roots,
concurrent writers, cached readers and external links are not exhaustively
checked. A clean report can never authorize deletion.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys

try:
    from . import bucket_file, reader_page_audit as audit, reader_page_migrate as migration
    from . import reader_page_redirects as redirects, reader_route_plan as routes
    from . import publish_reader_v3 as publication
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import bucket_file
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_redirects as redirects
    import reader_route_plan as routes
    import publish_reader_v3 as publication
    from reader_bucket_store import HubBucketStore


def decode(raw, compressed=False, limit=128 * 1024 * 1024):
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
            raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('reference root exceeds bounded preview size')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('reference root is not an object')
    return value


def explain(store, api, bucket, path):
    bucket_file.validate_target(bucket, path)
    if bucket not in {audit.BUCKET, audit.ASSETS}:
        raise ValueError('bounded Reader explanation supports only the two Reader output buckets')
    info = bucket_file.file_info(api, bucket, path)
    pointer, catalog = publication.current(store)
    canonical_raw = store.read_bytes(audit.ASSETS, audit.SIDECAR)
    canonical = audit.decode_sidecar(canonical_raw)
    pinned = publication.pinned_base(store, catalog, canonical) if pointer else canonical
    projected = publication.project_sidecar(pinned, catalog) if pointer else canonical
    legacy_raw = store.read_bytes(audit.ASSETS, migration.CATALOG)
    legacy = decode(legacy_raw)
    derived_raw = store.read_bytes(audit.BUCKET, audit.INDEX)
    derived = decode(derived_raw)
    roots = [('canonical-sidecar', canonical), ('pinned-sidecar', pinned), ('projected-sidecar', projected),
             ('v3-catalog', catalog), ('legacy-catalog', legacy), ('pdf-derived-index', derived)]
    matches = []
    for name, payload in roots:
        if name in {'canonical-sidecar', 'pinned-sidecar', 'projected-sidecar'}:
            owners = []
            for key, entry in payload['f'].items():
                if (bucket, path) in routes.refs(entry):
                    owners.append(key)
            matches.append({'root': name, 'direct_owner_count': len(owners), 'direct_owners': sorted(owners)})
        elif name == 'legacy-catalog':
            generations = []
            for generation, entry in (payload.get('generations') or {}).items():
                found = [ref for ref in entry.get('references', []) if isinstance(ref, dict)
                         and ref.get('bucket') == bucket and ref.get('path') == path]
                if found:
                    generations.append({'generation': generation, 'status': entry.get('status'), 'references': len(found)})
            matches.append({'root': name, 'generations': generations})
        elif name == 'v3-catalog':
            mapping = redirects.validated(payload)
            found = []
            if bucket == audit.BUCKET:
                if path in mapping:
                    found.append({'role': 'redirect-source', 'other': mapping[path]})
                found.extend({'role': 'redirect-target', 'other': old} for old, new in mapping.items() if new == path)
            matches.append({'root': name, 'redirects': found})
        else:
            selected = [row['key'] for row in payload.get('files', []) if isinstance(row, dict)
                        and isinstance(row.get('key'), str) and bucket == audit.BUCKET
                        and any(row.get(field) == path for field in ('new_path', 'manifest_path', 'path'))]
            matches.append({'root': name, 'direct_owner_count': len(selected), 'direct_owners': sorted(selected)})
    # Root versions are re-read, but this remains a non-atomic view.
    if (publication.current(store)[0] != pointer or store.read_bytes(audit.ASSETS, audit.SIDECAR) != canonical_raw
            or store.read_bytes(audit.ASSETS, migration.CATALOG) != legacy_raw
            or store.read_bytes(audit.BUCKET, audit.INDEX) != derived_raw):
        raise RuntimeError('Reader roots changed during bounded explanation; retry')
    return {'version': 1, 'kind': 'reader-exact-reference-explanation', 'bucket': bucket, 'path': path,
            'object': info, 'generation': pointer['generation'] if pointer else None,
            'canonical_sidecar_sha256': hashlib.sha256(canonical_raw).hexdigest(),
            'legacy_catalog_sha256': hashlib.sha256(legacy_raw).hexdigest(),
            'derived_index_sha256': hashlib.sha256(derived_raw).hexdigest(), 'roots': matches,
            'graph_complete': False, 'deletion_authorized': False,
            'limitations': ['direct references in selected roots only', 'nested manifests and unknown roots not scanned',
                            'external links and caches not scanned', 'non-atomic snapshot; later writes possible']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bucket')
    parser.add_argument('path')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        from huggingface_hub import HfApi
        report = explain(HubBucketStore(), HfApi(), args.bucket, args.path)
        raw = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(raw, encoding='utf-8')
        else:
            print(raw, end='')
        return 0
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
