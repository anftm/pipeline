"""Read-only preview for one exact historical static image (no apply mode).

Run with python -m scripts.reader_image_history_preflight --help. The ordinary
AVIF producer must be available in a clean, reviewed local checkout; this
command loads only its encoding helper, never its dispatcher. The plan is not
an Intent, a protection receipt, a cross-root fence, or deletion authority.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from . import build_reader_assets_v2_sidecar as sidecar, pilot_image_page_stream as images
from . import publish_reader_v3 as reader, reader_assets, reader_format_backlog as backlog
from . import reader_image_history as history, reader_route_control as control, reader_route_plan as routes
from . import search_publication_pointer as search
from .reader_bucket_store import S3BucketStore, retry_read, retry_s3
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore, strong_etag
from .reader_stream_index import shard_for

BUCKET = history.BUCKET
SHARDS = 8
MAX_IMAGE = 64 * 1024 * 1024
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 32_000_000
FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'bmp': 'BMP', 'webp': 'WEBP', 'tif': 'TIFF', 'tiff': 'TIFF'}


class ReadOnlyS3:
    """Bounded GET, never list, stream an entire catalog history, or write."""

    def __init__(self):
        from botocore.config import Config

        store = S3BucketStore()
        if store._namespace != 'vomebook':
            raise ValueError('expected the vomebook S3 namespace')
        store._config = store._config.merge(Config(retries={'mode': 'standard', 'total_max_attempts': 1}))
        self._client = store._client('vomebook')
        self.read_etags = {}

    def read_bytes(self, bucket, path):
        if bucket not in (BUCKET, sidecar.PDF_BUCKET, search.BUCKET):
            raise ValueError('unexpected preview bucket')
        limit = backlog.MAX_COMPRESSED if bucket == search.BUCKET else MAX_IMAGE

        def get():
            response = self._client.get_object(Bucket=bucket.split('/')[1], Key=path)
            body, length = response['Body'], response.get('ContentLength')
            try:
                if type(length) is not int or not 0 < length <= limit:
                    raise ValueError(f'preview object outside the read limit: {path}')
                raw = body.read(length + 1)
            finally:
                body.close()
            if len(raw) != length:
                raise ValueError(f'preview object length changed: {path}')
            self.read_etags[bucket, path] = response.get('ETag')
            return raw

        return retry_s3(get, attempts=2)


class Inputs:
    """Capture exact reads once; builder sees the same inputs on both passes."""

    def __init__(self, store):
        self.store = store
        self.read_etags = store.read_etags
        self.raw = {}
        self.size = 0
        self.override = None

    def read_bytes(self, bucket, path):
        ref = bucket, path
        if self.override is not None and ref == (BUCKET, history.CANONICAL):
            return self.override
        if ref not in self.raw:
            try:
                raw = self.store.read_bytes(*ref)
            except FileNotFoundError:
                raw = None
            if raw is not None:
                self.size += len(raw)
                if self.size > MAX_INPUT_BYTES:
                    raise ValueError('Reader input capture exceeds its byte budget')
            self.raw[ref] = raw
        if self.raw[ref] is None:
            raise FileNotFoundError(path)
        return self.raw[ref]

    def open(self, uri, mode):
        if mode != 'rb' or not uri.startswith('hf://buckets/'):
            raise ValueError('expected a read-only sidecar builder input')
        namespace, bucket, path = uri[len('hf://buckets/'):].split('/', 2)
        return io.BytesIO(self.read_bytes(f'{namespace}/{bucket}', path))

    def recheck(self):
        for ref, raw in self.raw.items():
            try:
                fresh = self.store.read_bytes(*ref)
            except FileNotFoundError:
                fresh = None
            if fresh != raw:
                raise ValueError(f'Reader input changed during preview: {ref[1]}')


def _decoded_image(raw, expected_format):
    from PIL import Image, ImageFile

    # The normal producer enables truncated images; a proof must not.
    truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if (image.format != expected_format or image.n_frames != 1
                    or not 0 < image.width * image.height <= MAX_PIXELS):
                raise ValueError(f'expected a bounded single-frame {expected_format} image')
            image.load()
            alpha = any(band.lower() == 'a' for band in image.getbands()) or 'transparency' in image.info
            return image.size, alpha
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = truncated


def _source(read_source, pin, expected_size):
    raw = read_source(pin, expected_size)
    if not isinstance(raw, bytes) or len(raw) != expected_size:
        raise ValueError('immutable source byte length differs')
    return history.Source(*pin, history._sha(raw), len(raw)), raw


def _build_sidecar(inputs):
    return sidecar.build(inputs, BUCKET, sidecar.PDF_BUCKET, 'reader-index/derived_pdf_manifest.json',
                         'reader-index/pdf_render_manifest.json', 'reader-index/pdf_ocr_manifest.json')


def _snapshot(snapshot):
    return {'sha256': snapshot.raw_sha256, 'bytes': len(snapshot.raw), 'etag': snapshot.etag}


def plan(*, store, read_source, source_head, encode_avif, repo, path, historical_revision, current_revision,
         search_generation, reader_generation, operation, search_get=backlog.search_daily.bounded_get):
    """Return review evidence, candidate index, canonical and pinned sidecars, and AVIF bytes."""
    key = routes.source_key(repo, path)
    for value, digits in ((historical_revision, 40), (current_revision, 40), (search_generation, 64),
                          (reader_generation, 64), (operation, 32)):
        history._hex(value, digits)
    extension = Path(path).suffix.lower().lstrip('.')
    if extension not in FORMATS:
        raise ValueError('preview requires one supported static non-GIF image')
    inputs = Inputs(store)
    checkpoints = ConditionalCheckpointStore(inputs)
    number = shard_for(key, SHARDS)
    shard_path = f'pages/image/index-v2-{number:02d}.json'
    parent, shard = (checkpoints.read(BUCKET, name) for name in (history.CANONICAL, shard_path))
    if parent.raw is None or shard.raw is None:
        raise ValueError('exact canonical and owning shard must both exist')
    canonical, owned = map(history._parent_index, (parent.raw, shard.raw))
    _, old = history._selected(canonical, key)
    if owned['kind'] != canonical['kind'] or any(
            shard_for(row['key'], SHARDS) != number for rows in (owned['files'], owned.get('failures', [])) for row in rows):
        raise ValueError('invalid owning image shard')
    pending = images.shard_baseline(owned, canonical, number, SHARDS)
    if (history._encode(history._selected(pending, key)[1]) != history._encode(old)
            or any(r['key'] == key for idx in (canonical, owned) for r in idx.get('failures', []))):
        raise ValueError('selected source has an unmerged checkpoint or recorded failure')

    expected_size = old.get('source_bytes')
    if (old.get('source_revision') != historical_revision or type(expected_size) is not int
            or not 0 < expected_size <= MAX_IMAGE):
        raise ValueError('historical source pin or size differs')
    search_raw, manifest_raw, corpus, revisions, observed = backlog.capture_search(store, get=search_get)
    record = backlog.source_records(corpus, revisions).get(key)
    if (search.decode_pointer(search_raw)['desired'] != search_generation or not record
            or record['revision'] != current_revision or record['extension'] != extension
            or record['bytes'] not in (0, expected_size) or source_head(repo) != current_revision):
        raise ValueError('current Search record, dataset head or generation differs from the reviewed pin')
    search_proof = {'pointer_sha256': history._sha(search_raw), 'manifest_sha256': history._sha(manifest_raw),
                    'record': record, 'consumers': observed}
    source, original = _source(read_source, (repo, path, historical_revision), expected_size)
    if historical_revision == current_revision:
        current = source
    else:
        current, _ = _source(read_source, (repo, path, current_revision), expected_size)
    history._same_source(source, current)
    size, alpha = _decoded_image(original, FORMATS[extension])
    webp_path = old.get('path')
    webp = history.Asset(webp_path.rpartition('/')[0] if isinstance(webp_path, str) else '', webp_path,
                         old.get('sha256'), old.get('bytes'))
    history._static(old, key, source, webp, 'webp')
    webp_raw = inputs.read_bytes(BUCKET, webp.path)
    if len(webp_raw) != webp.size or history._sha(webp_raw) != webp.sha256:
        raise ValueError('stored WebP differs from the exact historical row')
    dimensions, _ = _decoded_image(webp_raw, 'WEBP')

    base_raw = inputs.read_bytes(BUCKET, routes.SIDECAR)
    base = routes.decode_sidecar(base_raw)
    pointer, catalog = reader.current(inputs)
    if not pointer or pointer['generation'] != reader_generation or not catalog.get('base_sidecar'):
        raise ValueError('current Reader generation differs or lacks a pinned base')
    pinned = reader.pinned_base(inputs, catalog, {})
    before = reader.project_sidecar(pinned, catalog)
    if history._encode(before['f'].get(key)) != history._encode(history._route(webp)):
        raise ValueError('current Reader generation does not route the selected historical WebP')
    if history._encode(_build_sidecar(inputs)) != history._encode(base):
        raise ValueError('current canonical inputs do not rebuild the exact published sidecar')
    if history._encode(base['f'].get(key)) != history._encode(history._route(webp)):
        raise ValueError('canonical sidecar does not route the selected historical WebP')

    avif_raw = encode_avif(original)
    if not isinstance(avif_raw, bytes) or not 0 < len(avif_raw) <= MAX_IMAGE:
        raise ValueError('local AVIF output exceeds the preview limit')
    avif_dimensions, avif_alpha = _decoded_image(avif_raw, 'AVIF')
    if avif_dimensions != dimensions or avif_alpha != alpha:
        raise ValueError('AVIF dimensions or alpha differ from the expected image')
    digest = history._sha(avif_raw)
    root = images.image_root(extension, source.sha256, {'document.avif': digest})
    avif = history.Asset(root, root + '/document.avif', digest, len(avif_raw))
    row = {**old, 'profile': history.AVIF_PROFILE, 'root': root, 'path': avif.path, 'bytes': avif.size,
           'sha256': avif.sha256, 'source_revision': current.revision}
    # The old processing record cannot witness the new asset. An apply planner
    # must supply the new protection and a checkpoint timestamp under its fence.
    for field in ('processing_roots', 'checkpointed_at'):
        row.pop(field, None)
    candidate = history._replacement(canonical, key, history.row_digest(old), row, source=source, current=current,
                                     webp=webp, avif=avif, operation=operation)
    inputs.override = candidate
    rebuilt = _build_sidecar(inputs)
    history._projection_delta(key, webp, avif, base, rebuilt)
    proposed_raw = gzip.compress(reader.encode(rebuilt), compresslevel=9, mtime=0)
    packed, ref = control.prepare_base(catalog, proposed_raw)
    # Project against the *same* catalog rules so only routes are compared.
    # A later v3 publish would also advance generation metadata.
    after = reader.project_sidecar(routes.decode_sidecar(packed), catalog)
    before_sha, after_sha = history._projection_delta(key, webp, avif, before, after)

    # Recheck the search roots directly (not through the capture cache), and
    # all Reader builder inputs including optional absences and CAS validators.
    inputs.override = None
    inputs.recheck()
    if (strong_etag(store.read_etags[BUCKET, history.CANONICAL]) != parent.etag
            or strong_etag(store.read_etags[BUCKET, shard_path]) != shard.etag
            or store.read_bytes(search.BUCKET, search.KEY) != search_raw
            or store.read_bytes(search.BUCKET, f'generations/{search_generation}/manifest.json') != manifest_raw
            or source_head(repo) != current.revision):
        raise ValueError('CAS validator or pinned Search input changed during preview')
    report = {
        'kind': 'reader-image-history-preview', 'version': 1, 'key': key, 'operation': operation,
        'canonical': {'path': history.CANONICAL, **_snapshot(parent), 'target_sha256': history._sha(candidate)},
        'shard': {'path': shard_path, **_snapshot(shard),
                  'selected_row': next((r for r in owned['files'] if r['key'] == key), None)},
        'source': asdict(source), 'current_source': asdict(current), 'source_dimensions': size,
        'search_generation': search_generation, 'search_proof': search_proof,
        'search_sha256': history._sha(history._encode(search_proof)), 'reader_generation': reader_generation,
        'reader_pointer_sha256': history._sha(inputs.raw[BUCKET, reader.POINTER]),
        'reader_catalog_sha256': pointer['catalog']['sha256'], 'pinned_base_sha256': catalog['base_sidecar']['sha256'],
        'old_row': old, 'old_row_sha256': history.row_digest(old), 'target_row': row,
        'before_projection_sha256': before_sha, 'after_projection_sha256': after_sha,
        'before_route': before['f'][key], 'after_route': after['f'][key],
        'parent_sidecar_sha256': history._sha(base_raw), 'sidecar_sha256': history._sha(proposed_raw),
        'proposed_base': ref, 'changed_route_keys': [key],
        'reader_inputs': [{'bucket': b, 'path': p, 'sha256': None if raw is None else history._sha(raw)}
                          for (b, p), raw in sorted(inputs.raw.items())],
        'remote_writes': 0, 'publication_ready': False, 'deletion_authorized': False,
        'reader_consumers_verified': False, 'new_asset_protected': False,
    }
    return report, candidate, proposed_raw, packed, avif_raw


def _producer_encoder(checkout, revision):
    """Run the reviewed producer's local encoder, never its CLI."""
    # Import in a separate process so that both checkouts' scripts packages do
    # not alias each other. Require an exact clean tracked producer revision.
    checkout = checkout.resolve()
    history._hex(revision, 40)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()
    def check():
        if (git('rev-parse', '--show-toplevel') != str(checkout) or git('rev-parse', 'HEAD') != revision
                or git('status', '--porcelain', '--untracked-files=normal')):
            raise ValueError('AVIF encoder requires a clean checkout of the exact reviewed producer revision')

    check()
    git('ls-files', '--error-unmatch', 'scripts/pilot_image_page_stream.py', 'scripts/reader_image_avif.py')

    python_setup = 'import sys; sys.path[:0] = sys.argv[3:]; '
    paths = [str(checkout), *(str(Path(p).resolve()) for p in sys.path if p and Path(p).resolve() != Path.cwd())]

    def encode(raw):
        check()
        with tempfile.TemporaryDirectory(prefix='reader-image-preview-') as temp:
            source, target = Path(temp) / 'original', Path(temp) / 'document.avif'
            source.write_bytes(raw)
            subprocess.run([sys.executable, '-IB', '-c', python_setup +
                'from pathlib import Path; from PIL import Image, ImageFile; '
                'from scripts import pilot_image_page_stream as p, reader_image_avif as a\n'
                f'if a.PROFILE != {history.AVIF_PROFILE!r}: raise ValueError("unexpected AVIF producer profile")\n'
                'ImageFile.LOAD_TRUNCATED_IMAGES = False; '
                'im = Image.open(sys.argv[1]); p.encode_frame(im, Path(sys.argv[2]), codec="avif"); im.close()',
                str(source), str(target), *paths], cwd=checkout, check=True, timeout=120,
                env={k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL'} or k.startswith('LC_')})
            with target.open('rb') as stream:
                return stream.read(MAX_IMAGE + 1)
    return encode


def _plan_live(args, encode):
    import httpx
    from huggingface_hub import HfApi

    token = os.environ.get('HF_TOKEN')
    api = HfApi(token=token)
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        def read_source(pin, limit):
            repo, path, revision = pin
            headers = {'Authorization': f'Bearer {token}'} if token else {}
            with client.stream('GET', reader_assets.source_url(repo, revision, path), headers=headers) as response:
                if response.status_code != 200:
                    raise ValueError(f'pinned source GET returned HTTP {response.status_code}')
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(raw) + len(chunk) > limit:
                        raise ValueError('pinned source exceeds the expected byte length')
                    raw.extend(chunk)
                return bytes(raw)

        def head(repo):
            return retry_read(lambda: api.repo_info(repo, repo_type='dataset', timeout=30)).sha

        return plan(
            store=ReadOnlyS3(), read_source=read_source, source_head=head, encode_avif=encode,
            repo=args.repo, path=args.path, historical_revision=args.historical_revision,
            current_revision=args.current_revision, search_generation=args.search_generation,
            reader_generation=args.reader_generation, operation=args.operation)


def main(argv=None, *, build=_plan_live, load_encoder=_producer_encoder):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'path', 'historical-revision', 'current-revision', 'search-generation',
                 'reader-generation', 'operation', 'producer-revision'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--producer-checkout', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new local directory for preview artifacts')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if (args.output.exists() or args.output.is_symlink()
            or args.producer_checkout.resolve() in (output, *output.parents)):
        parser.error('--output must be new and outside the producer checkout')
    encode = load_encoder(args.producer_checkout, args.producer_revision)
    report, candidate, proposed, pinned, avif = build(args, encode)
    report['producer_revision'] = args.producer_revision
    output.mkdir(parents=False, exist_ok=False)
    for name, raw in (('candidate.json', candidate), ('sidecar.json.gz', proposed), ('pinned-base.json.gz', pinned),
                      ('document.avif', avif), ('report.json', reader.encode(report) + b'\n')):
        with (output / name).open('xb') as stream:
            stream.write(raw)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
