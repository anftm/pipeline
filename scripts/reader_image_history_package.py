"""Pack/reload the exact reviewed h.PNG inputs (not a mutation permit)."""
from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from . import publish_reader_v3 as v3, reader_image_history as image
from . import reader_image_history_runtime as runtime
from .reader_image_history_catalog import MAX_SIDECAR_BYTES
from .reader_stream_checkpoint_cas import MAX_INDEX, strong_etag

REVIEW = '50671e8927bf78f34411538e81f6971918e6655c73d2c8d803865122ed182ec0'
OPERATION = '17e9a04bc8bf4997809a061ea0ad59ec'
LIMIT = 32 * 1024 * 1024
FILE_LIMITS = {**dict.fromkeys(runtime.REPORTS, 256 * 1024), 'candidate.json': MAX_INDEX,
               'owning-shard-splice.json': MAX_INDEX, 'document.avif': 4 * 1024 * 1024,
               'pinned-base.json.gz': 8 * 1024 * 1024, 'sidecar.json.gz': MAX_SIDECAR_BYTES}
PARENTS = {'canonical.parent.json': MAX_INDEX, 'shard.parent.json': MAX_INDEX,
           'sidecar.parent.gz': MAX_SIDECAR_BYTES, 'pointer.parent.json': 1024 * 1024,
           'sidecar.parent.etag': 256}


def _review(directory, parents):
    review = runtime.review(directory, reviewed_sha256=REVIEW, canonical_parent=parents['canonical.parent.json'],
                            shard_parent=parents['shard.parent.json'], before_sidecar=parents['sidecar.parent.gz'],
                            sidecar_etag=parents['sidecar.parent.etag'].decode('ascii'),
                            pointer_parent=parents['pointer.parent.json'])
    if review.transition.canonical.operation != OPERATION:
        raise ValueError('unexpected h.PNG operation')
    return review


def _bounded(path, size):
    with path.open('rb') as source:
        raw = source.read(min(size, LIMIT) + 1)
    if not 0 < len(raw) <= min(size, LIMIT):
        raise ValueError(f'invalid migration input size: {path.name}')
    return raw


def pack(directory, store, output):
    """Read exact immutable-reviewed parents and write only a local ZIP."""
    directory = Path(directory)
    if (Path(output).exists() or Path(output).is_symlink()
            or image._sha(v3.encode([(name, image._sha(_bounded(directory / name, FILE_LIMITS[name])))
                                     for name in runtime.REPORTS])) != REVIEW):
        raise ValueError('output already exists or review reports differ')
    plan = json.loads(_bounded(directory / 'report.json', FILE_LIMITS['report.json']))
    canonical, shard = plan['canonical']['path'], plan['shard']['path']
    if (canonical, shard) != (image.CANONICAL, 'pages/image/index-v2-01.json'):
        raise ValueError('wrong migration roots')
    sidecar = 'reader-index/reader_assets.json.gz'
    parents = {}
    for name, path in zip(PARENTS, (canonical, shard, sidecar, v3.POINTER)):
        parents[name] = store.read_bytes(v3.ASSETS, path)
    parents['sidecar.parent.etag'] = strong_etag(store.read_etags[v3.ASSETS, sidecar]).encode('ascii')
    if any(store.read_etags[v3.ASSETS, path] != plan[field]['etag']
           for field, path in (('canonical', canonical), ('shard', shard))):
        raise ValueError('image checkpoint ETag differs from the review')
    _review(directory, parents)
    members = {name: _bounded(directory / name, limit) for name, limit in FILE_LIMITS.items()} | parents
    if sum(map(len, members.values())) + 4096 > LIMIT:
        raise ValueError('review package exceeds size ceiling')
    with Path(output).open('xb') as target, zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, members[name])
    with Path(output).open('rb') as result:
        return hashlib.file_digest(result, 'sha256').hexdigest()


def load(source, *, expected_sha256):
    """Load from a bounded seekable local/archive stream; use no extractall."""
    image._hex(expected_sha256)
    source.seek(0, 2)
    if not 0 < source.tell() <= LIMIT:
        raise ValueError('review package size outside its ceiling')
    source.seek(0)
    if hashlib.file_digest(source, 'sha256').hexdigest() != expected_sha256:
        raise ValueError('review package SHA-256 differs')
    source.seek(0)
    limits = FILE_LIMITS | PARENTS
    with zipfile.ZipFile(source) as archive, TemporaryDirectory(prefix='hpng-sealed-input-') as tmp:
        entries = archive.infolist()
        if (len(entries) != len(limits) or {x.filename for x in entries} != set(limits)
                or any(x.compress_type != zipfile.ZIP_STORED or x.flag_bits & 1
                       or not 0 < x.file_size <= min(limits[x.filename], LIMIT) for x in entries)
                or sum(x.file_size for x in entries) > LIMIT):
            raise ValueError('review package has unapproved entries, sizes, or compression')
        for name in FILE_LIMITS:
            (Path(tmp) / name).write_bytes(archive.read(name))
        return _review(Path(tmp), {name: archive.read(name) for name in PARENTS})
