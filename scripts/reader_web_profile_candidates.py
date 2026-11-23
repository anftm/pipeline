"""Optional, digest-pinned Web candidates reviewed against the same converter.

Only a digest in repository configuration admits a catalog. Current Search
revision is part of each input key: matching source bytes alone do not bind
same-tree images, styles or frames. This never bypasses live Reader replay.
"""
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
import re
import sys

from . import reader_web_managed_convert as convert, reader_web_managed_review as schema
from .reader_web_profile import WEB_PROFILE

PREFIX = 'reader-index/gc/web-profile-candidates/v1/'
MAX_INDEX = 2 * 1024**2
MAX_ENTRIES = 2500
HEX = re.compile(r'[a-f0-9]{64}')
CONVERTER_FILES = ('reader_web_managed_convert.py', 'convert_reader_assets.py', 'requirements.txt')


def digest(raw):
    return sha256(raw).hexdigest()


def converter_identity(directory=None):
    root = Path(__file__).resolve().parent if directory is None else directory
    return digest(convert.encode({
        'files': {name: digest((root / name).read_bytes()) for name in CONVERTER_FILES},
        'python': list(sys.version_info[:2]),
        'libraries': {name: version(name) for name in ('bleach','tinycss2','Pillow','lxml','webencodings')},
    }))


def source_identity(item, old):
    return {k: item[k] for k in ('repo', 'path', 'extension')} | {
        'source_revision': item['revision'], 'source_sha256': old['source_sha256']}


def input_key(source):
    return digest(convert.encode(source))


def object_path(sha):
    return PREFIX + 'html/' + sha + '.html'


def index_path(sha):
    return PREFIX + 'indexes/' + sha + '.json'


def load(get, pin, *, identity=None):
    """Load only a configured, exact catalog. Stale policies are cache misses."""
    if not pin:
        return {}
    if not isinstance(pin, str) or not HEX.fullmatch(pin):
        raise ValueError('invalid Web candidate catalog pin')
    raw = get(index_path(pin), MAX_INDEX)
    if not isinstance(raw, bytes) or len(raw) > MAX_INDEX or digest(raw) != pin:
        raise ValueError('Web candidate catalog bytes differ from the configured pin')
    import json
    value = json.loads(raw, object_pairs_hook=convert.unique_object)
    if (not isinstance(value, dict) or set(value) != {'version','profile','converter','entries'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['profile'], str) or not isinstance(value['converter'], str)
            or not HEX.fullmatch(value['converter']) or not isinstance(value['entries'], dict)
            or not 1 <= len(value['entries']) <= MAX_ENTRIES):
        raise ValueError('invalid Web candidate catalog')
    if value['profile'] != WEB_PROFILE or value['converter'] != (identity or converter_identity()):
        return {}
    for key, row in value['entries'].items():
        if (not HEX.fullmatch(key) or not isinstance(row, dict)
                or set(row) != {'sha256','bytes','omissions'}
                or not isinstance(row['sha256'], str) or not HEX.fullmatch(row['sha256'])
                or type(row['bytes']) is not int or not 0 < row['bytes'] <= schema.MAX_DOC
                or not isinstance(row['omissions'], dict) or len(row['omissions']) > 32
                or any(not re.fullmatch(r'[a-z_]{1,40}', k) or type(v) is not int or not 0 < v <= 100000
                       for k,v in row['omissions'].items())):
            raise ValueError('invalid reviewed Web candidate entry')
    return value['entries']


def reuse(get, entries, source):
    entry = entries.get(input_key(source))
    if entry is None:
        return None
    raw = get(object_path(entry['sha256']), entry['bytes'])
    if not isinstance(raw, bytes) or len(raw) != entry['bytes'] or digest(raw) != entry['sha256']:
        raise ValueError('reviewed Web candidate body differs')
    raw.decode('utf-8', errors='strict')
    return raw, entry['omissions']