"""Read-only S3 byte identities for the reviewed single MHT replacement."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path

from .reader_image_history_publish_s3 import CATALOG, SIDECAR, LegacyRoots
from .reader_page_cas import ConditionalPageStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .publish_reader_v3 import ASSETS, POINTER, CentralHubStore

INDEX = 'documents/web/mht/index.json'
OLD_ROOT = 'documents/web/mht/7d1b2ea8999ae052d537f9f92a28329d10ec3537ff9282222ac7c3485494b83b/'
PINS = {
    'catalog': ('9c0530f63de52e89a5ef4c86a31e34621a72d19d6e11ee50c3e1aa01e551e753', 317944578),
    'sidecar': ('588150fe8c0a7973b468d704e2ba99dcb13d5d051ca4515525d953ad12ff5ea6', 2154574),
    'index': ('e2fa8a43f441f750a5e8b0396d2cfded82b981f66a80b42fab70a88405c865ff', None),
    'old_html': ('c77dd3e2fe5f2c84995052454e2baa235ac992ab3e66f1d142a56c26b1e24af5', 297528),
    'old_manifest': ('b637a169f53242d6a491759f2f5613939eb1728d22321d33bd2bc7f614017d6a', None),
}
GEN = '31ecd55f9d42d1ad5979f87b1b37be323686204c7f900c953daaf61e27b8e8ce'


def review(index_store, pointer_store, legacy, hub, s3):
    def check(name, raw=None, identity=None):
        if identity is None and not isinstance(raw, bytes):
            raise ValueError('missing reviewed MHT input: ' + name)
        digest, size = identity if identity is not None else (sha256(raw).hexdigest(), len(raw))
        wanted, exact = PINS[name]
        if digest != wanted or (exact is not None and size != exact):
            raise ValueError('reviewed MHT parent drift: ' + name)

    index = index_store.read(ASSETS, INDEX)
    check('index', index.raw)
    if hub.read_bytes(ASSETS, INDEX) != index.raw:
        raise ValueError('MHT index differs between S3 and Hub')
    pointer, etag = pointer_store.read_for_update(ASSETS, POINTER)
    if json.loads(pointer).get('generation') != GEN or hub.read_bytes(ASSETS, POINTER) != pointer:
        raise ValueError('Reader pointer differs between the reviewed S3 and Hub views')
    cat = legacy.observe(CATALOG)
    check('catalog', identity=(cat.sha256, cat.size))
    side = legacy.observe(SIDECAR)
    check('sidecar', identity=(side.sha256, side.size))
    if sha256(hub.read_bytes(ASSETS, SIDECAR)).hexdigest() != side.sha256:
        raise ValueError('legacy sidecar differs between S3 and Hub')
    for name, member in [('old_html', 'document.html'), ('old_manifest', 'document-manifest.json')]:
        path = OLD_ROOT + member
        raw = s3.read_bytes(ASSETS, path)
        check(name, raw)
        if hub.read_bytes(ASSETS, path) != raw:
            raise ValueError('historical MHT differs between S3 and Hub')
    for path, observed in ((CATALOG, cat), (SIDECAR, side)):
        latest = s3._client('vomebook').head_object(Bucket='reader-assets-v2', Key=path)
        if (latest.get('ETag'), latest.get('ContentLength')) != (observed.etag, observed.size):
            raise ValueError('legacy root changed during read-only collection: ' + path)
    if index_store.read(ASSETS, INDEX) != index or pointer_store.read_for_update(ASSETS, POINTER) != (pointer, etag):
        raise ValueError('authority changed during read-only collection')
    return {'version': 1, 'catalog': asdict(cat), 'sidecar': asdict(side),
            'index_sha256': index.raw_sha256, 'index_etag': index.etag,
            'reader_generation': GEN, 'pointer_sha256': sha256(pointer).hexdigest(), 'pointer_etag': etag,
            'remote_writes': 0, 'publication_ready': False}


def main():
    if (os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        raise RuntimeError('requires an exact central hosted read-only run')
    index = ConditionalCheckpointStore()
    store = index.store
    hub = CentralHubStore()
    result = review(index, ConditionalPageStore(hub, s3=store), LegacyRoots(store._client('vomebook')), hub, store)
    output = Path('output/mht-s3-preflight.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print('Exact MHT S3 parents and old bytes verified; no writes performed', flush=True)


if __name__ == '__main__':
    main()
