"""Bounded candidate archive for an exact central EPUB preview or release."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
from zipfile import ZipFile

from scripts.reader_ebook_rebuild_review import read_bundle
from scripts.reader_route_plan import source_key

PREFIX = 'reader-index/gc/ebook-exact-inputs/v1/'
MAX_ZIP = 48 * 1024**2
MAX_EXPANDED = 64 * 1024**2
MAX_MEMBER = 16 * 1024**2
MAX_MEMBERS = 4096


def require(ok, message):
    if not ok:
        raise ValueError(message)


def unpack(raw, digest, output):
    require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
            and 0 < len(raw) <= MAX_ZIP and hashlib.sha256(raw).hexdigest() == digest,
            'EPUB candidate archive SHA or length mismatch')
    require(output.is_dir() and not output.is_symlink() and not any(output.iterdir()),
            'EPUB candidate output must be an empty private directory')
    with ZipFile(io.BytesIO(raw)) as z:
        records = z.infolist()
        paths = set()
        require(len(records) <= MAX_MEMBERS and sum(info.file_size for info in records) <= MAX_EXPANDED,
                'EPUB candidate archive count or total bytes exceeded')
        for info in records:
            name = info.filename
            path = PurePosixPath(name)
            mode = (info.external_attr >> 16) & 0xffff
            allowed = name == 'selection.json' or (
                len(path.parts) >= 3 and path.parts[0] == 'bundles' and re.fullmatch(r'[0-9a-f]{64}', path.parts[1]))
            require(allowed and name == path.as_posix() and '\\' not in name
                    and not path.is_absolute() and all(part not in ('', '.', '..') for part in path.parts)
                    and not any(ord(c) < 32 or ord(c) == 127 for c in name)
                    and name not in paths and not info.is_dir() and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                    and 0 < info.file_size <= MAX_MEMBER and not info.flag_bits & 1,
                    'invalid or duplicate EPUB candidate archive member')
            paths.add(name)
        require('selection.json' in paths, 'EPUB candidate selection missing')
        for info in records:
            target = output/info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as source, target.open('xb') as out:
                payload = source.read(info.file_size + 1)
                require(len(payload) == info.file_size, 'EPUB archive member size changed')
                out.write(payload)
    selection = json.loads((output/'selection.json').read_bytes())
    require(isinstance(selection, dict) and set(selection) == {'version','kind','sources'}
            and selection['version'] == 1 and selection['kind'] == 'reader-exact-epub-candidates'
            and isinstance(selection['sources'], list) and 1 <= len(selection['sources']) <= 3,
            'invalid EPUB candidate selection')
    sources = {}
    for row in selection['sources']:
        require(isinstance(row, dict) and set(row) == {'repo','path','manifest_sha256'}, 'invalid EPUB candidate row')
        key = source_key(row['repo'], row['path'])
        require(row['path'].lower().endswith('.epub') and key not in sources, 'invalid or duplicate EPUB candidate key')
        ident = hashlib.sha256(key.encode()).hexdigest()
        directory = output/'bundles'/ident
        _manifest, members, _version = read_bundle(directory)
        require(next(member['sha256'] for member in members if member['path'] == 'chapter-manifest.json')
                == row['manifest_sha256'], 'EPUB candidate manifest SHA-256 differs')
        sources[key] = directory
    require(set((output/'bundles').iterdir()) == set(sources.values()), 'unexpected extra EPUB candidate bundle')
    return sources
