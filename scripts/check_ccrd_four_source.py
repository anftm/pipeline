#!/usr/bin/env python3
"""Compare the upstream CW:0 text to the live four-source manifest, read-only.

A mismatch is diagnostic, not a publication plan. This command never uploads,
activates, migrates, or removes anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

try:
    from . import ccrd_consumer_inspection as consumer, ccrd_lifecycle as lifecycle, ccrd_source_override as source
    from . import ccrd_source_snapshot as snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import ccrd_consumer_inspection as consumer
    import ccrd_lifecycle as lifecycle
    import ccrd_source_override as source
    import ccrd_source_snapshot as snapshot
    from reader_bucket_store import HubBucketStore


def check(store, download=source.download_override, observe=consumer.observe, *, capture=None):
    before = store.read_bytes(lifecycle.BUCKET, 'current.json')
    manifest = json.loads(before)
    if manifest.get('format') != 2:
        raise ValueError('read-only check requires the four-source manifest')
    refs = lifecycle.references(manifest, 'current.json')
    recorded = manifest.get('source_overrides', {}).get(str(source.CORPUS_PATH))
    if not isinstance(recorded, dict):
        raise ValueError('current four-source generation lacks the CW:0 identity')
    lifecycle.sha(recorded.get('sha256'))
    if type(recorded.get('bytes')) is not int or recorded['bytes'] <= 0:
        raise ValueError('current CW:0 byte size is invalid')
    if capture is None:
        with tempfile.TemporaryDirectory(prefix='ccrd-source-readonly-') as directory:
            target = Path(directory) / 'source.txt'
            observed = download(target)
            raw = target.read_bytes()
    else:
        raw, observed = capture()
        if not isinstance(observed, dict) or observed.get('source_stable') is not True:
            raise ValueError('pinned CW:0 source is not stable')
    if not isinstance(raw, bytes) or len(raw) > source.MAX_OVERRIDE_BYTES or not isinstance(observed, dict):
        raise ValueError('invalid CW:0 source snapshot')
    actual = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    if not raw or any(observed.get(field) != value for field, value in actual.items()):
        raise ValueError('downloaded CW:0 identity is inconsistent')
    raw.decode('utf-8')
    recorded_identity = {field: recorded[field] for field in ('sha256', 'bytes')}
    observed_consumer = observe(manifest['generation'], recorded_identity)
    if store.read_bytes(lifecycle.BUCKET, 'current.json') != before:
        raise RuntimeError('current CCRD manifest changed during the check')
    result = {'version': 1, 'kind': 'ccrd-four-source-readonly-check',
            'generation': manifest['generation'], 'current_sha256': hashlib.sha256(before).hexdigest(),
            'sources': sorted(manifest['databases']), 'declared_resources': len(refs),
            'recorded': recorded_identity, 'upstream': actual, 'source_changed': actual != recorded_identity,
            'consumer': observed_consumer, 'remote_writes': 0, 'publication_ready': False,
            'scope': 'four-source manifest, upstream snapshot and CW:0 consumer bytes; other consumer bytes unverified'}
    if capture is not None:
        result['source_observation'] = observed
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    import os
    token = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')
    report = check(HubBucketStore(), capture=lambda: snapshot.capture(token))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
