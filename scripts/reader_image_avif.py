"""The AVIF producer contract shared by publication and reference walkers."""
from __future__ import annotations

import hashlib
import json
import re


PROFILE = 'reader-image-avif-q70-444-v1'
BUCKET = 'vomebook/reader-assets-v2'
FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'bmp': 'BMP',
           'webp': 'WEBP', 'tif': 'TIFF', 'tiff': 'TIFF'}
TIFF = {'tif', 'tiff'}
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_PAGES = 100_000
SHA = re.compile(r'[0-9a-f]{64}\Z')
PAGE_MANIFEST = re.compile(r'pages/image/(tiff?)/[0-9a-f]{64}/page-manifest\.json\Z')
OBJECT = re.compile(r'pages/image/(?:(?:jpg|jpeg|png|bmp|webp|tiff?)/[0-9a-f]{64}/document'
                    r'|tiff?/[0-9a-f]{64}/pages/page-(?!000000)[0-9]{6})\.avif\Z')


def digest(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def image_root(extension, source_digest, files):
    """Keep existing Reader URLs but bind the root to every output digest."""
    identity = json.dumps({'version': 1, 'source_sha256': source_digest, 'files': files},
                          sort_keys=True, separators=(',', ':')).encode()
    return f'pages/image/{extension}/{hashlib.sha256(identity).hexdigest()}'


def is_avif_entry(row):
    return (row.get('profile') == PROFILE or 'image_format' in row
            or any(isinstance(row.get(field), str) and row[field].lower().endswith('.avif')
                   for field in ('path', 'manifest')))


def _image_size(value):
    return type(value) is int and 0 < value <= MAX_IMAGE_BYTES


def avif_route(row, bucket):
    """Validate the canonical AVIF row without imposing new rules on WebP."""
    ext, root, source = row.get('extension'), row.get('root'), row.get('source_sha256')
    if (row.get('profile') != PROFILE or bucket != BUCKET or row.get('bucket') != bucket
            or not isinstance(ext, str) or ext not in FORMATS or not digest(source)
            or not isinstance(root, str) or not re.fullmatch(rf'pages/image/{ext}/[0-9a-f]{{64}}', root)
            or type(row.get('page_count')) is not int):
        raise ValueError('invalid AVIF image index row')
    if row.get('mode') == 'image':
        path = root + '/document.avif'
        if (row.get('path') != path or 'manifest' in row or 'image_format' in row or row['page_count'] != 1
                or not digest(row.get('sha256')) or not _image_size(row.get('bytes'))
                or root != image_root(ext, source, {'document.avif': row['sha256']})):
            raise ValueError('invalid AVIF document identity')
    else:
        path = root + '/page-manifest.json'
        if (row.get('mode') != 'page-stream' or ext not in TIFF or row.get('manifest') != path
                or 'path' in row or row.get('image_format') != 'avif' or not 2 <= row['page_count'] <= MAX_PAGES):
            raise ValueError('invalid AVIF TIFF index row')
    return {'s': 2, 'm': 'i', 'p': path, 'b': bucket}


def avif_pages(payload, path):
    """Return the ordered TIFF page identities; None means a legacy manifest."""
    if not path.startswith('pages/image/'):
        return None
    if 'image_format' not in payload and not (
            isinstance(payload.get('pages'), list)
            and any(isinstance(e, dict) and isinstance(e.get('path'), str)
                    and e['path'].lower().endswith('.avif') for e in payload['pages'])):
        return None
    match = PAGE_MANIFEST.fullmatch(path)
    entries, count = payload.get('pages'), payload.get('page_count')
    if (not match or payload.get('kind') != 'image-page-stream'
            or type(payload.get('version')) is not int or payload['version'] != 1
            or payload.get('image_format') != 'avif' or payload.get('source_extension') != match[1]
            or not digest(payload.get('source_sha256')) or type(count) is not int or not 2 <= count <= MAX_PAGES
            or not isinstance(entries, list) or len(entries) != count
            or any(not isinstance(e, dict) or e.get('path') != f'pages/page-{n:06d}.avif'
                   or not digest(e.get('sha256')) or not _image_size(e.get('bytes'))
                   for n, e in enumerate(entries, 1))):
        raise ValueError(f'invalid AVIF image page manifest: {path}')
    return entries


def keeps_existing_profile(prior, entry):
    return (prior.get('profile') == PROFILE and entry.get('profile') != PROFILE
            and (not digest(entry.get('source_sha256')) or prior.get('source_sha256') == entry['source_sha256']))


def bundle_members(row, bucket, read):
    """Bind a TIFF manifest to its row and immutable root before exposing it."""
    path = avif_route(row, bucket)['p']
    if row['mode'] == 'image':
        return {path: (row['sha256'], row['bytes'])}
    raw = read(bucket, path)
    if not 0 < len(raw) <= MAX_MANIFEST_BYTES:
        raise ValueError('invalid AVIF TIFF manifest size')
    payload = json.loads(raw)
    pages = avif_pages(payload, path) if isinstance(payload, dict) else None
    if (pages is None or payload['source_sha256'] != row['source_sha256']
            or len(pages) != row['page_count'] or type(row.get('bytes')) is not int
            or sum(e['bytes'] for e in pages) != row['bytes']):
        raise ValueError('AVIF TIFF manifest differs from its index')
    identities = {e['path']: e['sha256'] for e in pages}
    identities['page-manifest.json'] = hashlib.sha256(raw).hexdigest()
    if image_root(row['extension'], row['source_sha256'], identities) != row['root']:
        raise ValueError('AVIF TIFF manifest differs from its immutable root')
    return {path: (identities['page-manifest.json'], len(raw)),
            **{f"{row['root']}/{e['path']}": (e['sha256'], e['bytes']) for e in pages}}
