"""Fetch a digest-bound exact web review from a fixed private bucket path."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import argparse
import json

from . import publish_reader_v3 as v3
from . import reader_web_exact_apply as apply
from .reader_web_exact_plan import BUCKET, _is_sha

PREFIX = 'reader-index/gc/web-exact-inputs/v1/'
MEMBERS = {'review.json', 'canonical.json', 'old.json', 'new.json', 'document.html',
           'document-manifest.json', 'original', 'pointer.json', 'sidecar-before.gz', 'sidecar-after.gz', 'sidecar-etag'}
MAX_PACKED = 12 * 1024 * 1024
MAX_EXPANDED = 16 * 1024 * 1024


def unpack(raw, expected_sha, root):
    if not _is_sha(expected_sha) or not 0 < len(raw) <= MAX_PACKED or sha256(raw).hexdigest() != expected_sha:
        raise ValueError('immutable web review ZIP identity differs')
    with ZipFile(BytesIO(raw)) as zipped:
        entries = zipped.infolist()
        if (len(entries) != len(MEMBERS) or {e.filename for e in entries} != MEMBERS
                or sum(e.file_size for e in entries) > MAX_EXPANDED or any(e.file_size <= 0 for e in entries)):
            raise ValueError('unexpected web review ZIP members or expansion')
        for entry in entries:
            # Fixed flat names; never allow a ZIP to choose a path, symlink or mode.
            with zipped.open(entry) as source, (root / entry.filename).open('xb') as target:
                data=source.read(entry.file_size+1)
                if len(data)!=entry.file_size:
                    raise ValueError('web review ZIP member size differs')
                target.write(data)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resource-json',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    options=json.loads(args.resource_json)
    if (not isinstance(options,dict) or options.keys()!={'bundle_sha256','review_sha256','phase','external_writers_stopped'}
            or any(not _is_sha(options[k]) for k in ('bundle_sha256','review_sha256'))
            or options['phase'] not in {'apply','resume-index','resume-legacy','observe-v3'}
            or options['external_writers_stopped'] is not True):
        raise ValueError('exact web operation requires two reviewed digests, phase, and separate writer confirmation')
    primary=v3.CentralHubStore()
    apply.guard_window(primary,True)
    path=PREFIX+options['bundle_sha256']+'.zip'
    if not 0 < primary.stat(BUCKET,path) <= MAX_PACKED:
        raise ValueError('exact web package outside bounded size')
    from .reader_image_history_store import S3ClaimStore
    # Reuse the bounded single-response reader, not a HEAD followed by an
    # unbounded GET. This wrapper never accepts a caller-supplied object key.
    raw,_=S3ClaimStore()._get(path,maximum=MAX_PACKED)
    if raw is None:
        raise ValueError('exact web review ZIP not found')
    with TemporaryDirectory(prefix='reader-web-exact-') as folder:
        root=Path(folder)
        unpack(raw,options['bundle_sha256'],root)
        report=apply.run(root,options['review_sha256'],phase=options['phase'],external_writers_stopped=True)
    with args.output.open('xb') as output:
        output.write(v3.encode(report))

if __name__=='__main__':main()
