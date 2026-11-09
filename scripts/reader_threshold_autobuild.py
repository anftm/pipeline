"""Automatically build bounded Reader conversion shadows above a backlog threshold.

This stage performs no bucket or site mutation. A trigger binds the actual
published source and both Reader consumers, recomputes the backlog, chooses a
small number of genuinely changed or unmapped sources, then downloads and
validates complete converted bytes. It refuses ambiguous source identities.
The separate publication protocol and external-writer coordination remain
necessary; a successful local build is not an ingestion receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

import httpx
from huggingface_hub import HfApi, HfFileSystem

try:
    from . import reader_assets, reader_epub_plan, reader_epub_shadow
    from . import reader_format_backlog as backlog, reader_stream_index, search_snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import reader_assets
    import reader_epub_plan
    import reader_epub_shadow
    import reader_format_backlog as backlog
    import reader_stream_index
    import search_snapshot
    from reader_bucket_store import HubBucketStore


MAX_BATCH = 3
MAX_SOURCE_BYTES = 16 * 1024 * 1024
PRODUCTION_THRESHOLD = 100
SUPPORTED = {'epub', 'jpg', 'mp3'}


def decision(summary):
    if (not isinstance(summary, dict) or summary.get('version') != 1
            or summary.get('kind') != 'reader-format-backlog'
            or type(summary.get('threshold')) is not int or summary['threshold'] != PRODUCTION_THRESHOLD
            or summary.get('remote_writes') != 0 or summary.get('dispatches') != 0
            or summary.get('publication_ready') is not False or summary.get('external_writers_verified') is not False
            or not isinstance(summary.get('formats'), list)):
        raise ValueError('invalid read-only Reader backlog summary')
    receipts = summary.get('reader_consumers')
    generation = summary.get('reader_generation')
    import re
    if (not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{64}', generation)
            or not isinstance(summary.get('search_generation'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', summary['search_generation'])):
        raise ValueError('Reader/search generations are not exact identities')
    if (not isinstance(receipts, dict) or set(receipts) != {'pages', 'hf'}
            or any(not isinstance(receipts[s], dict) or receipts[s].get('projection_verified') is not True
                   or receipts[s].get('generation') != generation for s in ('pages', 'hf'))):
        raise ValueError('both Reader consumers must match the observed generation')
    rows = {}
    for row in summary['formats']:
        if not isinstance(row, dict) or not isinstance(row.get('extension'), str) or row['extension'] in rows:
            raise ValueError('invalid or duplicate format summary')
        rows[row['extension']] = row
    if not SUPPORTED <= rows.keys():
        raise ValueError('required Reader format summaries are missing')
    result = []
    for extension in sorted(SUPPORTED):
        row = rows[extension]
        values = (row.get('not_mapped'), row.get('source_content_changed'), row.get('definite_backlog'))
        if (any(type(v) is not int or v < 0 for v in values)
                or values[0] + values[1] != values[2]
                or row.get('threshold_exceeded') is not (values[2] > PRODUCTION_THRESHOLD)):
            raise ValueError('Reader backlog threshold identity differs')
        if row.get('extension') != extension or row.get('family') != {'epub':'ebook','jpg':'image','mp3':'media'}[extension]:
            raise ValueError('Reader format classification differs')
        if not row['threshold_exceeded']:
            continue
        if (row.get('all_source_identities_checked') is True
                and row.get('conversion_dependencies_checked') is True
                and row.get('source_identity_unknown') == 0
                and row.get('pending_shard_checkpoint') == 0
                and row.get('recorded_failure_current') == 0):
            result.append(extension)
    return result


def blocked(summary):
    """Return unresolved formats; never hide one behind a successful build."""
    eligible = set(decision(summary))
    return sorted(row['extension'] for row in summary['formats']
                  if row.get('threshold_exceeded') is True and row['extension'] not in eligible)


def _read_json_file(path):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError('backlog report must be a bounded ordinary file')
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError('backlog report must be a JSON object')
    return value


def write_decision(summary, output):
    if output.exists() or output.is_symlink():
        raise ValueError('threshold decision output must not exist')
    formats = decision(summary)
    result = {'version': 1, 'kind': 'reader-threshold-local-build-decision',
              'search_generation': summary['search_generation'], 'reader_generation': summary['reader_generation'],
              'summary_sha256': hashlib.sha256(search_snapshot.canonical(summary)).hexdigest(),
              'threshold': PRODUCTION_THRESHOLD, 'formats': formats, 'blocked_formats': blocked(summary),
              'max_sources_per_format': MAX_BATCH, 'remote_writes': 0, 'publication_ready': False}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2);stream.write('\n')
    return result


def _file_info(value):
    if value is None:
        return None
    path = backlog._item_field(value, 'path')
    size = backlog._item_field(value, 'size')
    if not isinstance(path, str) or type(size) is not int or not 0 < size <= MAX_SOURCE_BYTES:
        return None
    lfs = backlog._lfs_identity(value)
    blob = backlog._blob_identity(value)
    if not lfs and not blob:
        return None
    return {'path': path, 'bytes': size, 'proof': 'lfs-sha256' if lfs else 'git-blob',
            'identity': lfs or blob}


def select_unmapped(extension, records, routes, canonical, shards, api):
    """Pick exact missing JPG/MP3 sources; ignore old revision churn."""
    if extension not in {'jpg', 'mp3'}:
        raise ValueError('unsupported missing-only conversion format')
    expected_kind = 'image-stream-index' if extension == 'jpg' else 'media-stream-index'
    if not isinstance(canonical, dict) or canonical.get('version') != 1 or canonical.get('kind') != expected_kind:
        raise ValueError('canonical format index is unavailable')
    reader_stream_index.validate_index(canonical, 'format canonical')
    occupied = set()
    for field in ('files', 'failures'):
        keys = [entry['key'] for entry in canonical.get(field, [])]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate canonical format source')
        occupied.update(keys)
    if not isinstance(shards, dict) or set(shards) != set(range(8)):
        raise ValueError('all eight format shards must be observed')
    for number, value in shards.items():
        if not value:
            continue
        if value.get('version') != 1 or value.get('kind') != expected_kind:
            raise ValueError('invalid format shard')
        reader_stream_index.validate_index(value, 'format shard')
        # Historical media shards may contain foreign old copies. A key found
        # anywhere is conservatively excluded, even if it belongs elsewhere.
        for field in ('files', 'failures'):
            keys = [entry['key'] for entry in value.get(field, [])]
            if len(keys) != len(set(keys)):
                raise ValueError('duplicate format shard source')
            occupied.update(keys)
    candidates = [item for item in records.values() if item['extension'] == extension
                  and item['key'] not in routes and item['key'] not in occupied]
    candidates.sort(key=lambda item: (item['bytes'] <= 0, item['bytes'], item['repo'], item['path']))
    # Bound metadata work and downloads while leaving unverified candidates in
    # the backlog. Missing/oversized/non-content identities are not selected.
    examined = candidates[:100]
    requested = {(item['repo'], item['revision'], item['path']) for item in examined}
    metadata = backlog._metadata(api, requested)
    selected = []
    object_keys = set()
    for item in examined:
        info = _file_info(metadata.get((item['repo'], item['revision'], item['path'])))
        if info is None or info['path'] != item['path']:
            continue
        identity = (info['proof'], info['identity'])
        if identity in object_keys:
            continue
        object_keys.add(identity)
        selected.append({**item, 'source_bytes': info['bytes'], 'source_proof': info['proof'],
                         'source_identity': info['identity'], 'shard': reader_stream_index.shard_for(item['key'], 8)})
        if len(selected) == MAX_BATCH:
            break
    if not selected:
        raise ValueError('no bounded unmapped source has a verified exact identity')
    return selected


def _source_digest(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected['source_bytes']:
        raise ValueError('local source does not match pinned size')
    sha = hashlib.sha256();git = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            sha.update(chunk);git.update(chunk)
    if expected.get('source_proof') not in {'lfs-sha256', 'git-blob'}:
        raise ValueError('unknown source identity proof')
    value = sha.hexdigest() if expected['source_proof'] == 'lfs-sha256' else git.hexdigest()
    if value != expected['source_identity']:
        raise ValueError('downloaded source differs from its pinned content identity')
    return sha.hexdigest()


def _converted_file(path):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
        raise ValueError('converted output missing or exceeds bounds')
    data = path.read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _parser_environment():
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
    env['PYTHONHASHSEED'] = '0'
    return env


def convert_local(source, target, extension):
    """Use a token-free bounded subprocess for untrusted JPG or MP3 bytes."""
    import subprocess
    if extension not in {'jpg', 'mp3'}:
        raise ValueError('unsupported local shadow converter')
    subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--_convert', extension,
                    str(source), str(target)], env=_parser_environment(), check=True, timeout=240)


def _convert_child(extension, source, target):
    import resource
    import shutil
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SOURCE_BYTES, MAX_SOURCE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (180, 181))
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    if extension == 'jpg':
        from PIL import Image, ImageFile
        Image.MAX_IMAGE_PIXELS = 100_000_000
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with Image.open(source) as image:
            if image.format not in {'JPEG', 'MPO'} or not 0 < image.width <= 16000 or not 0 < image.height <= 16000:
                raise ValueError('source is not a bounded JPEG')
            converted = image.convert('RGB')
            try:
                if max(converted.size) > 8192:
                    scale = 8192 / max(converted.size)
                    resized = converted.resize((max(1, round(converted.width * scale)),
                                                max(1, round(converted.height * scale))), Image.Resampling.LANCZOS)
                    converted.close();converted = resized
                converted.save(target, 'WEBP', quality=85, method=6)
            finally:
                converted.close()
        with Image.open(target) as verified:
            verified.verify()
    elif extension == 'mp3':
        try:
            from . import convert_reader_assets as converter
        except ImportError:
            import convert_reader_assets as converter
        if not converter.browser_native_media(source, 'mp3', 'audio'):
            raise ValueError('MP3 source is not browser-native; transcoding requires the media workflow')
        converter.validate_native_media_output(source, 'audio')
        shutil.copyfile(source, target)
        converter.validate_native_media_output(target, 'audio')
    else:
        raise ValueError('unsupported local shadow converter')


def _index_paths(extension):
    if extension == 'jpg':
        return 'pages/image/index-v2.json', 'pages/image/index-v2-{number:02d}.json'
    if extension == 'mp3':
        return 'media/audio/index.json', 'media/audio/index-{number:02d}.json'
    raise ValueError('unsupported missing-only format')


def inspect_unmapped(extension, *, hub, api, fs, client):
    pointer, catalog = backlog.publication.current(hub)
    if not pointer or not catalog.get('base_sidecar'):
        raise ValueError('Reader v3 pointer or pinned base is missing')
    base = backlog.publication.pinned_base(hub, catalog, {})
    routes = backlog.publication.project_sidecar(base, catalog)['f']
    for surface in ('pages', 'hf'):
        receipt = backlog.publication.observe_consumer(hub, surface, client)
        if receipt.get('generation') != pointer['generation'] or receipt.get('projection_verified') is not True:
            raise ValueError('Reader site is not on the exact pinned projection')
    search_raw, _manifest, corpus, revisions, _ = backlog.capture_search(hub)
    records = backlog.source_records(corpus, revisions)
    canonical_path, shard_template = _index_paths(extension)
    canonical = backlog.matrix.read_json(fs, backlog.READER_BUCKET, canonical_path)
    shards = {i: backlog.matrix.read_json(fs, backlog.READER_BUCKET, shard_template.format(number=i)) for i in range(8)}
    selected = select_unmapped(extension, records, routes, canonical, shards, api)
    for repo, expected in sorted(revisions.items()):
        info = backlog.retry_read(lambda: api.repo_info(repo, repo_type='dataset', timeout=30))
        if getattr(info, 'sha', None) != expected:
            raise ValueError('source head changed during shadow selection')
    for surface in ('pages', 'hf'):
        receipt = backlog.publication.observe_consumer(hub, surface, client)
        if receipt.get('generation') != pointer['generation'] or receipt.get('projection_verified') is not True:
            raise ValueError('Reader site changed during shadow selection')
    if (backlog.publication.current(hub)[0] != pointer
            or hub.read_bytes(backlog.SEARCH_BUCKET, backlog.CURRENT) != search_raw
            or backlog.matrix.read_json(fs, backlog.READER_BUCKET, canonical_path) != canonical):
        raise ValueError('Reader/search root changed during shadow selection')
    if any(backlog.matrix.read_json(fs, backlog.READER_BUCKET, shard_template.format(number=i)) != snapshot
           for i, snapshot in shards.items()):
        raise ValueError('Reader format shard changed during shadow selection')
    return {'version': 1, 'kind': 'reader-unmapped-shadow-plan', 'extension': extension,
            'search_generation': backlog.search_publication_pointer.decode_pointer(search_raw)['desired'],
            'reader_generation': pointer['generation'],
            'canonical_index_sha256': hashlib.sha256(search_snapshot.canonical(canonical)).hexdigest(),
            'shard_indexes_sha256': {str(i): hashlib.sha256(search_snapshot.canonical(x)).hexdigest() for i,x in shards.items()},
            'sources': selected, 'remote_writes': 0, 'publication_ready': False}


def build_unmapped(plan, output, *, token, fetch=reader_epub_shadow.download, convert=convert_local):
    if output.exists() or output.is_symlink():
        raise ValueError('local conversion output already exists')
    if plan.get('extension') not in {'jpg', 'mp3'} or not 1 <= len(plan.get('sources', [])) <= MAX_BATCH:
        raise ValueError('invalid bounded local conversion plan')
    output.mkdir(parents=True, mode=0o700)
    results = []
    for number, row in enumerate(plan['sources']):
        work = output / str(number);work.mkdir()
        source = work / ('source.' + plan['extension'])
        fetch(reader_assets.source_url(row['repo'], row['revision'], row['path']), source, token)
        digest = _source_digest(source, row)
        target = work / ('document.webp' if plan['extension'] == 'jpg' else 'audio.mp3')
        convert(source, target, plan['extension'])
        result = _converted_file(target)
        if plan['extension'] == 'mp3' and result['sha256'] != digest:
            raise ValueError('native MP3 shadow differs from its source bytes')
        results.append({'key': row['key'], 'revision': row['revision'], 'source_sha256': digest,
                        'source_bytes': row['source_bytes'], 'output': result, 'mode': plan['extension']})
    return {'version': 1, 'kind': 'reader-unmapped-local-shadow', 'extension': plan['extension'],
            'search_generation': plan['search_generation'], 'reader_generation': plan['reader_generation'],
            'plan_sha256': hashlib.sha256(search_snapshot.canonical(plan)).hexdigest(),
            'sources': results, 'remote_writes': 0, 'index_written': False,
            'publication_ready': False, 'external_writers_verified': False}


def build_epub(output, *, token, hub, api, fs, client):
    # Select the first shard with a qualifying exact source. Every inspect is
    # fresh; unchanged EPUB sources are never converted merely for a revision.
    operation = secrets.token_hex(16)
    for shard in range(8):
        try:
            plan = reader_epub_plan.inspect(hub=hub, api=api, fs=fs, client=client,
                                            shard=shard, operation=operation)
            break
        except ValueError as error:
            if str(error) != 'requested EPUB shard has no eligible exact sources':
                raise
    else:
        raise ValueError('EPUB threshold exceeded but no exact candidate exists')
    # Bound downloaded source sizes before launching the legacy EPUB parser.
    requested = {(row['repo'], row['source_revision'], row['path']) for row in plan['sources']}
    infos = backlog._metadata(api, requested)
    for row in plan['sources']:
        info = _file_info(infos.get((row['repo'], row['source_revision'], row['path'])))
        if info is None:
            raise ValueError('selected EPUB source is too large or lacks a pinned content identity')
    report = reader_epub_shadow.build(plan, output, token=token)
    preview = reader_epub_shadow.staging_plan(plan, report, output)
    current = reader_epub_plan.inspect(hub=hub, api=api, fs=fs, client=client,
                                       shard=shard, operation=operation)
    if current != plan:
        raise ValueError('EPUB exact plan changed during local conversion')
    return {'version': 1, 'kind': 'reader-threshold-epub-local-shadow', 'extension': 'epub',
            'search_generation': plan['search_generation'], 'reader_generation': plan['reader_generation'],
            'plan_sha256': plan['plan_sha256'], 'staging_sha256': preview['staging_sha256'],
            'sources': report['sources'], 'remote_writes': 0, 'index_written': False,
            'publication_ready': False, 'external_writers_verified': False}



def verify_completion(decision_value, output):
    if (not isinstance(decision_value, dict) or decision_value.get('kind') != 'reader-threshold-local-build-decision'
            or not isinstance(decision_value.get('formats'), list)
            or not isinstance(decision_value.get('blocked_formats'), list)):
        raise ValueError('invalid threshold completion decision')
    missing = []
    for extension in decision_value['formats']:
        report = output / extension / 'report.json'
        try:
            value = _read_json_file(report)
            if (value.get('extension') != extension or value.get('remote_writes') != 0
                    or value.get('publication_ready') is not False or not value.get('sources')):
                raise ValueError('incomplete local conversion')
        except (OSError, ValueError, json.JSONDecodeError):
            missing.append(extension)
    if missing or decision_value['blocked_formats']:
        raise ValueError('threshold candidates need attention: missing=' + ','.join(missing)
                         + ' blocked=' + ','.join(decision_value['blocked_formats']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    decide = sub.add_parser('decide');decide.add_argument('--summary', type=Path, required=True);decide.add_argument('--output', type=Path, required=True)
    verify = sub.add_parser('verify');verify.add_argument('--decision', type=Path, required=True);verify.add_argument('--output', type=Path, required=True)
    build = sub.add_parser('build');build.add_argument('--extension', choices=sorted(SUPPORTED), required=True)
    build.add_argument('--summary', type=Path, required=True);build.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'verify':
        verify_completion(_read_json_file(args.decision), args.output)
        print(json.dumps({'kind': 'reader-threshold-local-builds-verified', 'remote_writes': 0, 'publication_ready': False}))
        return 0
    initial = _read_json_file(args.summary)
    if args.command == 'decide':
        result = write_decision(initial, args.output)
    else:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError('local conversion output already exists')
        if args.extension not in decision(initial):
            raise ValueError('requested format is not an eligible threshold candidate')
        token = os.environ.get('HF_TOKEN');hub = HubBucketStore();api = HfApi(token=token);fs = HfFileSystem(token=token)
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            # Never trust a workflow artifact as fresh evidence or authorization.
            current, _details = backlog.inspect(hub=hub, api=api, fs=fs, client=client)
            if (args.extension not in decision(current) or current['search_generation'] != initial['search_generation']
                    or current['reader_generation'] != initial['reader_generation']):
                raise ValueError('Reader backlog changed before local conversion')
            if args.extension == 'epub':
                result = build_epub(args.output, token=token, hub=hub, api=api, fs=fs, client=client)
                if (result['search_generation'] != current['search_generation']
                        or result['reader_generation'] != current['reader_generation']):
                    raise ValueError('Reader generation changed during EPUB build')
            else:
                plan = inspect_unmapped(args.extension, hub=hub, api=api, fs=fs, client=client)
                if (plan['search_generation'] != current['search_generation']
                        or plan['reader_generation'] != current['reader_generation']):
                    raise ValueError('Reader source generations changed before build')
                result = build_unmapped(plan, args.output, token=token)
                later = inspect_unmapped(args.extension, hub=hub, api=api, fs=fs, client=client)
                if later != plan:
                    raise ValueError('Reader source or index changed during local build')
        report = args.output / 'report.json'
        with report.open('x', encoding='utf8') as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2);stream.write('\n')
    print(json.dumps({'kind': result['kind'], 'formats': result.get('formats'),
                      'extension': result.get('extension'), 'remote_writes': 0,
                      'publication_ready': False}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--_convert':
        _convert_child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
    else:
        raise SystemExit(main())
