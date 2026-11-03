#!/usr/bin/env python3
"""Read-only exact CHM chapter/resource bundle checksum verifier."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import re


ROOT = re.compile(r'chapters/ebook/chm/[0-9a-f]{64}/([0-9a-f]{16})')


def verify_bundle(store, bucket, root, files, *, workers=8):
    match = ROOT.fullmatch(root)
    if not match or not files or len(files) != len(set(files)):
        raise ValueError('invalid CHM chapter bundle inventory')
    prefix = root + '/'
    if any(not isinstance(path, str) or not path.startswith(prefix)
           or '\\' in path or any(part in {'', '.', '..'} for part in path.split('/')) for path in files):
        raise ValueError('CHM chapter inventory escaped its root')
    if root + '/chapter-manifest.json' not in files:
        raise ValueError('CHM chapter bundle manifest is missing')
    def read(path):
        raw = store.read_bytes(bucket, path)
        return path[len(prefix):], hashlib.sha256(raw).digest(), len(raw)
    content = hashlib.sha256()
    count = total = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        for relative, digest, size in pool.map(read, sorted(files)):
            content.update(relative.encode('utf-8') + b'\0')
            content.update(digest)
            count += 1
            total += size
    if content.hexdigest()[:16] != match[1]:
        raise ValueError('CHM immutable chapter bundle checksum differs')
    return {'root': root, 'objects': count, 'bytes': total, 'bundle_version': match[1], 'remote_mutations': 0}
