"""Simulated deployed HF/Pages Reader projections for page migration tests."""
import gzip
import hashlib

from scripts import publish_reader_v3 as publication


class FakeResponse:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass
    def raise_for_status(self):
        pass
    def iter_bytes(self):
        yield self.body


class Consumers:
    def __init__(self, store, *, stale=None, missing=None):
        self.store, self.stale, self.missing = store, stale, missing
    def stream(self, method, url, **_kwargs):
        if method != 'GET':
            raise AssertionError(method)
        if self.missing and self.missing in url:
            raise OSError('site unavailable')
        pointer, catalog = publication.current(self.store)
        sidecar = publication.project_sidecar(publication.pinned_base(self.store, catalog, {}), catalog)
        if self.stale and self.stale in url:
            sidecar['f']['unexpected'] = {'s': 4}
        if '/api/reader-v3-status' in url:
            return FakeResponse(publication.encode({'version': 1, 'surface': 'hf', 'active': True,
                'generation': pointer['generation'], 'catalog_sha256': pointer['catalog']['sha256'],
                'files': len(catalog['files'])}))
        if '/api/reader-assets' in url:
            sidecar['f'] = {key: ({'s': 4} if value.get('s') == 4 else value)
                            for key, value in sidecar['f'].items()}
            return FakeResponse(publication.encode(sidecar))
        packed = gzip.compress(publication.encode(sidecar), mtime=0)
        if 'reader_v3_receipt.json' in url:
            return FakeResponse(publication.encode({'version': 1, 'surface': 'pages', 'active': True,
                'generation': pointer['generation'], 'catalog_sha256': pointer['catalog']['sha256'],
                'files': len(catalog['files']), 'reader_contract': 'pdf-reading-v3-v1',
                'sidecar_sha256': hashlib.sha256(packed).hexdigest()}))
        if 'reader_assets.json.gz' in url:
            return FakeResponse(packed)
        raise AssertionError(url)
