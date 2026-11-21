import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image
import yaml

from scripts import finalize_image_index as finalizer, pilot_image_page_stream as image
from scripts import reader_image_avif as avif, reader_image_canary as canary
from scripts import reader_bucket, reader_gc_graph, reader_lifecycle, reader_stream_index
from scripts.reader_bucket_store import S3BucketStore
from scripts.reader_stream_checkpoint_cas import Snapshot
from tests.test_reader_format_workflow_automation import inputs_for, job_runs


REV, OLD_REV = 'a' * 40, 'b' * 40
SEARCH, PARENT = 'c' * 64, 'd' * 64
REPO = 'VoiceOfML/dataset'
BUCKET = avif.BUCKET


def source(frames, extension):
    output = io.BytesIO()
    frames[0].save(output, 'GIF' if extension == 'gif' else avif.FORMATS[extension],
                   save_all=len(frames) > 1, append_images=frames[1:])
    return output.getvalue()


def convert(tmp_path, frames, extension, **kwargs):
    raw = source(frames, extension)
    path = f'books/source.{extension}'
    item = {'repo': REPO, 'path': path, 'extension': extension, 'bytes': len(raw), 'revision': REV}
    with patch.object(image, 'download', side_effect=lambda _url, path, _token, **_: path.write_bytes(raw)):
        return image.build_one(item, tmp_path, None, BUCKET, codec='avif', **kwargs)


def check_still_avif_alpha_and_distinct_root(tmp_path):
    frame = Image.new('RGBA', (32, 24), (24, 52, 108, 155))
    row, files = convert(tmp_path / 'out', [frame], 'png')
    path = row['path']
    data = Path(files[path]).read_bytes()
    with Image.open(io.BytesIO(data)) as decoded:
        assert (decoded.format, decoded.size, decoded.n_frames) == ('AVIF', frame.size, 1)
        assert decoded.getpixel((12, 12))[3] == 155
    assert row['profile'] == avif.PROFILE
    assert row['sha256'] == hashlib.sha256(data).hexdigest()
    assert row['root'] != avif.image_root('png', row['source_sha256'], {'document.webp': row['sha256']})
    assert avif.avif_route(row, BUCKET) == {'s': 2, 'm': 'i', 'p': path, 'b': BUCKET}


def check_tiff_page_sequence_hashes_root_and_graph_from_sidecar(tmp_path):
    frames = [Image.new('RGBA', (30, 24), rgba) for rgba in [(255, 0, 0, 100), (0, 0, 255, 205)]]
    row, files = convert(tmp_path, frames, 'tiff')
    path = row['manifest']
    raw = Path(files[path]).read_bytes()
    manifest = json.loads(raw)
    members = avif.bundle_members(row, BUCKET, lambda _bucket, member: Path(files[member]).read_bytes())
    assert (row['mode'], row['page_count'], manifest['image_format']) == ('page-stream', 2, 'avif')
    assert [p['path'] for p in manifest['pages']] == ['pages/page-000001.avif', 'pages/page-000002.avif']
    for frame, entry in zip(frames, manifest['pages']):
        object_path = row['root'] + '/' + entry['path']
        with Image.open(files[object_path]) as decoded:
            assert decoded.format == 'AVIF'
            assert decoded.getpixel((12, 12))[3] == frame.getpixel((12, 12))[3]
            assert (decoded.getpixel((12, 12))[0] > decoded.getpixel((12, 12))[2]) == (frame.getpixel((12, 12))[0] == 255)
        assert members[object_path] == (entry['sha256'], entry['bytes'])
    assert set(members) == set(files)
    # Same valid manifest schema, but altered bytes must not match the root.
    with TestCase().assertRaisesRegex(ValueError, 'immutable root'):
        avif.bundle_members(row, BUCKET, lambda *_: raw + b' ')

    fake = Mock(read_bytes=lambda _bucket, _path: raw)
    assert S3BucketStore.manifest_references(fake, BUCKET, {path}) == {path: set(files) - {path}}
    graph = reader_gc_graph.ReferenceGraph(fake)
    missing = max(set(files) - {path})
    graph.set_inventory(BUCKET, set(files) - {missing})
    graph.process((BUCKET, canary.SIDECAR), {'v': 1, 'f': {row['key']: avif.avif_route(row, BUCKET)}})
    assert (BUCKET, path) in graph.pending
    graph.process((BUCKET, path), manifest)
    assert (BUCKET, missing) in graph.deferred
    assert {(BUCKET, member) for member in files} <= graph.references
    assert not graph.blockers
    for invalid in ({**manifest, 'pages': list(reversed(manifest['pages']))},
                    {k: v for k, v in manifest.items() if k != 'image_format'}):
        graph.process((BUCKET, path), invalid)
        assert any('invalid AVIF image page manifest' in error for error in graph.blockers)
        fake.read_bytes = lambda *_: json.dumps(invalid).encode()
        with TestCase().assertRaisesRegex(ValueError, 'invalid AVIF image page manifest'):
            S3BucketStore.manifest_references(fake, BUCKET, {path})
    fake.read_bytes = lambda *_: b'{"page_count":2}'
    assert S3BucketStore.manifest_references(fake, BUCKET, {path}) == {
        path: {f"{row['root']}/pages/page-{i:06}.webp" for i in (1, 2)}}


def check_gif_and_animated_webp_or_png_are_outside_canary(tmp_path):
    frames = [Image.new('RGB', (32, 24), color) for color in ('red', 'blue')]
    for ext in ('gif', 'webp', 'png'):
        with TestCase().assertRaisesRegex(ValueError, 'non-GIF|animated'):
            convert(tmp_path / ext, frames, ext)
    with TestCase().assertRaisesRegex(ValueError, 'page count limit'):
        convert(tmp_path / 'tif', frames, 'tif', max_tiff_pages=1)


def check_source_checksum_is_rechecked_before_encode(tmp_path):
    frames = [Image.new('RGB', (20, 20), 'blue')]
    raw = source(frames, 'png')
    with patch.object(image, 'encode_frame') as encode:
        with TestCase().assertRaisesRegex(ValueError, 'unchanged'):
            convert(tmp_path, frames, 'png', previous_source_sha256=hashlib.sha256(raw).hexdigest())
        encode.assert_not_called()
    api = Mock()
    blob = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
    api.get_paths_info.return_value = [SimpleNamespace(path='books/source.png', size=len(raw), lfs=None, blob_id=blob)]
    corpus = {'v': 2, 'rp': [REPO], 'fd': [['books']], 'rc': [[0, 'source', 'png', 0, len(raw), 0]]}
    item = canary.source_record(corpus, {REPO: REV}, repo=REPO, path='books/source.png', api=api)
    assert item['git_blob'] == blob
    assert api.get_paths_info.call_args.kwargs['revision'] == REV
    with patch.object(image, 'download', side_effect=lambda _, path, _token, **_kw: path.write_bytes(b'x' * len(raw))) as download:
        with TestCase().assertRaisesRegex(ValueError, 'Git blob'):
            image.build_one(item, tmp_path / 'bad', None, BUCKET, codec='avif')
        assert download.call_args.kwargs == {'exact_bytes': len(raw)}


def check_canary_rejects_existing_mapped_only_equal_or_staged_sources(tmp_path):
    row, _ = convert(tmp_path, [Image.new('RGB', (20, 20))], 'jpg')
    key = row['key']
    route = avif.avif_route(row, BUCKET)
    prior = {**row, 'path': 'pages/image/jpg/' + 'e' * 64 + '/document.webp',
             'source_revision': OLD_REV, 'source_sha256': 'f' * 64}
    prior.pop('profile')
    base = {'f': {key: {**route, 'p': prior['path']}}}
    canonical = {'files': [prior]}
    empty = {'files': []}
    item = {'key': key, 'revision': REV, 'source_sha256': row['source_sha256']}
    assert canary.previous_source(item, canonical, empty, base) == prior['source_sha256']
    assert canary.previous_source(item, empty, empty, {'f': {}}) is None
    for args in ((item, empty, empty, base),
                 ({**item, 'revision': OLD_REV}, canonical, empty, base),
                 ({**item, 'source_sha256': prior['source_sha256']}, canonical, empty, base),
                 (item, canonical, {'files': [{**row, 'checkpointed_at': 'now'}]}, base),
                 (item, canonical, {**empty, 'failures': [{'key': key, 'source_revision': REV}]}, base)):
        with TestCase().assertRaisesRegex(ValueError, 'canary requires|historical Reader route|unmerged checkpoint'):
            canary.previous_source(*args)
    shard = reader_stream_index.shard_for(key, 8)
    ordinary = {**prior, 'source_sha256': row['source_sha256'], 'checkpointed_at': 'now'}
    with_avif = {'version': 1, 'kind': 'image-stream-index', 'files': [row]}
    assert finalizer.merge(with_avif, [(shard, {'files': [ordinary]})]) == with_avif
    assert image.shard_baseline({'files': [ordinary]}, with_avif, shard, 8)['files'] == [row]


def check_workflow_gate_blocks_retries_and_other_lanes():
    workflow = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/reader-image-stream.yml').read_text())
    inputs = workflow[True]['workflow_dispatch']['inputs']
    assert inputs['avif_canary']['default'] == ''
    assert inputs['boost_canary']['default'] is False
    assert inputs['apply']['default'] is False
    jobs = workflow['jobs']
    job = jobs['avif-canary']
    needs = {'plan': {'result': 'success', 'outputs': {'matrix': '{"include":[{"extension":"jpg","shard":0}]}'}},
             'build': {'result': 'success'}}
    assert job_runs(jobs['plan'], inputs_for(workflow), needs)
    for boost in (False, True):
        ordinary = inputs_for(workflow, apply=True, boost_canary=boost)
        named = {**ordinary, 'avif_canary': '{"path":"a path.png"}'}
        for name in ('plan', 'build', 'finalize'):
            assert job_runs(jobs[name], ordinary, needs)
            assert not job_runs(jobs[name], named, needs)
        assert not job_runs(job, ordinary, {})
        assert job_runs(job, named, {})  # Mixed intent fails closed in the shell gate below.
    assert job['concurrency'] == jobs['finalize']['concurrency']
    step = job['steps'][-1]
    assert step['env']['READER_IMAGE_AVIF_ENABLED'] == '${{ vars.READER_IMAGE_AVIF_ENABLED }}'
    assert step['env']['BOOST_CANARY'] == '${{ inputs.boost_canary }}'
    shell = 'python() { printf "%s\\n" "$@"; };\n' + step['run']
    env = {'READER_IMAGE_AVIF_ENABLED': 'true', 'GITHUB_RUN_ATTEMPT': '1', 'AUTOMATION_ID': '',
           'DEFER_UNIFY': 'true', 'LIMIT': '1', 'RETRY_FAILURES': 'false', 'APPLY': 'false',
           'BOOST_CANARY': 'false', 'SOURCE': '{"path":"a path.png"}',
           'SEARCH_GENERATION': SEARCH, 'EXPECTED_SHA': REV}
    for override in ({}, {'APPLY': 'true'}, {'READER_IMAGE_AVIF_ENABLED': ''}, {'GITHUB_RUN_ATTEMPT': '2'},
                     {'BOOST_CANARY': 'true', 'APPLY': 'true'}, {'AUTOMATION_ID': 'auto'},
                     {'DEFER_UNIFY': 'false'}, {'LIMIT': '2'}, {'RETRY_FAILURES': 'true'},
                     {'SEARCH_GENERATION': ''}, {'EXPECTED_SHA': ''}):
        result = subprocess.run(['bash', '-c', shell], env={**env, **override}, text=True, capture_output=True)
        allowed = not override or override == {'APPLY': 'true'}
        assert (result.returncode == 0) == allowed
        assert ('--apply' in result.stdout) == (override == {'APPLY': 'true'})
    context = {'READER_IMAGE_AVIF_ENABLED': 'true', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '1234',
               'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_WORKFLOW_REF': 'anftm/pipeline/.github/workflows/reader-image-stream.yml@refs/heads/main',
               'GITHUB_WORKFLOW_SHA': REV, 'GITHUB_SHA': REV, 'GITHUB_EVENT_NAME': 'workflow_dispatch'}
    assert canary.require_context(context, lambda: REV)[0] == REV
    for key in context:
        with TestCase().assertRaisesRegex(ValueError, 'first-attempt'):
            canary.require_context({**context, key: ''}, lambda: REV)
    with TestCase().assertRaisesRegex(ValueError, 'first-attempt'):
        canary.require_context(context, lambda: OLD_REV)


class MemoryStore:
    def __init__(self, objects, events, corrupt_mime=False):
        self.objects = objects
        self.events = events
        self.read_etags = {}
        self.corrupt_mime = corrupt_mime

    def _location(self, bucket):
        return bucket.split('/')

    def _client(self, _namespace):
        return self

    def read_bytes(self, bucket, path):
        try:
            raw, _ = self.objects[bucket, path]
        except KeyError:
            raise FileNotFoundError(path) from None
        self.read_etags[bucket, path] = '"' + hashlib.sha256(raw).hexdigest() + '"'
        return raw

    def read_graph_json(self, bucket, path, parser):
        return parser([self.read_bytes(bucket, path)])

    def put_object(self, *, Bucket, Key, Body, ContentType, **condition):
        bucket = f'vomebook/{Bucket}'
        if condition == {'IfNoneMatch': '*'}:
            assert (bucket, Key) not in self.objects
        else:
            assert condition == {'IfMatch': self.read_etags[bucket, Key]}
        self.events.append((Key, ContentType))
        mime = 'application/octet-stream' if self.corrupt_mime and Key.endswith('.avif') else ContentType
        self.objects[bucket, Key] = (Body if isinstance(Body, bytes) else Body.read(), mime)

    def get_object(self, *, Bucket, Key):
        try:
            raw, mime = self.objects['vomebook/' + Bucket, Key]
        except KeyError:
            raise FileNotFoundError(Key) from None
        return {'Body': io.BytesIO(raw), 'ContentLength': len(raw), 'ContentType': mime}


def check_canary_checkpoint_publication_and_protection(tmp_path, behavior):
    extension, path = 'tif', 'books/scan.tif'
    key = f'{REPO}\0{path}'
    frames = [Image.new('RGB', (18, 16), c) for c in ('yellow', 'green')]
    raw = source(frames, extension)
    item = {'key': key, 'repo': REPO, 'path': path, 'extension': extension, 'revision': REV, 'bytes': len(raw)}
    old_root = f'pages/image/tif/{"a" * 64}'
    old = {'key': key, 'bucket': BUCKET, 'extension': extension, 'mode': 'image', 'root': old_root,
           'path': old_root + '/document.webp', 'source_sha256': 'e' * 64, 'source_revision': OLD_REV}
    new_source = behavior == 'new-source'
    prior_key = REPO + '\0books/other.tif' if new_source else key
    old['key'] = prior_key
    canonical = {'version': 1, 'kind': 'image-stream-index', 'files': [old]}
    base = {'v': 1, 'f': {prior_key: {'s': 2, 'm': 'i', 'p': old['path'], 'b': BUCKET}}}
    catalog, old_generation = reader_lifecycle.catalog_generation(base)
    packed = gzip.compress(canary.reader.encode(base), mtime=0)
    search_pointer = b'search pointer'
    objects = {(BUCKET, canary.SIDECAR): (packed, 'application/gzip'),
               (BUCKET, canary.CATALOG): (canary.reader.encode(catalog), 'application/json'),
               (BUCKET, old['path']): (b'old webp', 'image/webp'),
               (BUCKET, finalizer.CANONICAL): (canary.reader.encode(canonical), 'application/json'),
               (canary.backlog.SEARCH_BUCKET, canary.backlog.CURRENT): (search_pointer, 'application/json'),
               (canary.backlog.SEARCH_BUCKET, f'generations/{SEARCH}/manifest.json'): (b'manifest', 'application/json')}
    events = []
    hub = MemoryStore(objects, events, corrupt_mime=behavior == 'wrong-mime')
    api = Mock()
    api.repo_info.return_value = SimpleNamespace(sha=REV if behavior != 'source-drift' else OLD_REV)
    reader_pointer = {'generation': PARENT}
    checkpoint = canary.ConditionalCheckpointStore(hub)
    original_read = checkpoint.read
    checkpoint.read = lambda bucket, path: (original_read(bucket, path) if path == finalizer.CANONICAL
                                            else Snapshot(None, None, None))

    def read_index(_fs, bucket, path):
        if path in ('documents/text/index.json', 'reader-index/derived_pdf_manifest.json'):
            files = [{'key': REPO + '\0new.txt', 'extension': 'txt', 'object': 'documents/text/extra'}] if (
                path.startswith('documents/text/') and behavior == 'unrelated-route') else []
            return {'files': files}
        if path == finalizer.CANONICAL:
            return json.loads(hub.read_bytes(bucket, path))
        return {'files': []}

    def publish_bytes(path, value, token):
        events.append((path, 'publish'))
        if behavior != 'readback-failure':
            objects[BUCKET, path] = (value, 'application/gzip')

    def publish_catalog(value, token):
        updated, generation = reader_lifecycle.catalog_generation(value, catalog)
        objects[BUCKET, canary.CATALOG] = (canary.reader.encode(updated), 'application/json')
        objects[BUCKET, f'reader-index/generations/{generation}/reader_assets.json.gz'] = (
            gzip.compress(canary.reader.encode(value)), 'application/gzip')
        events.append((canary.CATALOG, 'publish'))
        return generation

    reader_catalog = {'files': {}, 'base_sidecar': True}
    if behavior == 'active-redirects':
        reader_catalog['derived_pdf_redirects'] = {'old.pdf': 'new.pdf'}
    with patch.object(canary.reader, 'current', return_value=(reader_pointer, reader_catalog)), \
         patch.object(canary.reader, 'pinned_base', return_value=base), \
         patch.object(canary.reader, 'verify_generation'), \
         patch.object(canary.backlog, 'capture_search', return_value=(search_pointer, b'manifest', {}, {}, {})), \
         patch.object(canary.backlog.search_publication_pointer, 'decode_pointer', return_value={'desired': SEARCH}), \
         patch.object(canary, 'source_record', return_value=item), \
         patch.object(image, 'download', side_effect=lambda _url, output, _token, **_: output.write_bytes(raw)), \
         patch.object(canary.sidecar, 'read_json', side_effect=read_index), \
         patch.object(reader_bucket, 'publish_bytes', side_effect=publish_bytes), \
         patch.object(reader_bucket, 'publish_catalog', side_effect=publish_catalog):
        args = dict(repo=REPO, path=path, revision=REV, operation='f' * 32, search_generation=SEARCH,
                    reader_parent=PARENT, token=None, hub=hub, api=api, github=Mock(), fs=None,
                    checkpoint=checkpoint, work=tmp_path, apply=behavior != 'preview', http=None)
        errors = {'wrong-mime': 'MIME', 'source-drift': 'changed before publication',
                  'unrelated-route': 'more than the exact', 'readback-failure': 'sidecar readback differs',
                  'active-redirects': 'no redirects'}
        if behavior in errors:
            with TestCase().assertRaisesRegex(ValueError, errors[behavior]):
                canary.run(**args)
        else:
            result = canary.run(**args)
            assert result['route']['p'].endswith('/page-manifest.json')
            assert result['applied'] == (behavior != 'preview')
    if behavior in ('source-drift', 'unrelated-route', 'preview', 'active-redirects'):
        assert not events
    else:
        protection = events[0][0]
        assert protection.startswith('reader-index/processing/')
        assert events[1][0].endswith('/pages/page-000001.avif')
        assert events[1][1] == 'image/avif'
        assert json.loads(hub.read_bytes(BUCKET, protection))['status'] == (
            'released' if behavior in ('apply', 'new-source') else 'processing')
    if behavior in ('apply', 'new-source'):
        assert [path for path, _ in events[4:7]] == [finalizer.CANONICAL, canary.SIDECAR, canary.CATALOG]
        assert events[7][0].startswith('reader-index/gc/retirements/')
        assert events[8][0] == protection
        assert events[3][1] == 'application/json'
        manifest = result['route']['p']
        rows = {row['key']: row for row in json.loads(hub.read_bytes(BUCKET, finalizer.CANONICAL))['files']}
        assert rows[key]['manifest'] == manifest
        if new_source:
            assert rows[prior_key] == old
        updated = json.loads(hub.read_bytes(BUCKET, canary.CATALOG))
        assert updated['generations'][old_generation]['references'] == [{'bucket': BUCKET, 'path': old['path']}]
        assert {ref['path'] for ref in updated['generations'][updated['current']['generation']]['references']} == (
            {manifest, old['path']} if new_source else {manifest})
    if behavior == 'wrong-mime':
        assert json.loads(hub.read_bytes(BUCKET, finalizer.CANONICAL)) == canonical
    assert hub.read_bytes(BUCKET, old['path']) == b'old webp'


class ImageAVIFTests(TestCase):
    def temp_check(self, check, *args):
        with TemporaryDirectory() as directory:
            check(Path(directory), *args)

    def test_still_avif_alpha_and_distinct_root(self):
        self.temp_check(check_still_avif_alpha_and_distinct_root)

    def test_tiff_sequence_manifest_root_and_reference_walkers(self):
        self.temp_check(check_tiff_page_sequence_hashes_root_and_graph_from_sidecar)

    def test_animation_and_oversize_tiff_are_excluded(self):
        self.temp_check(check_gif_and_animated_webp_or_png_are_outside_canary)

    def test_source_checksum_rechecked_before_encode(self):
        self.temp_check(check_source_checksum_is_rechecked_before_encode)

    def test_canary_eligibility_and_profile_reuse(self):
        self.temp_check(check_canary_rejects_existing_mapped_only_equal_or_staged_sources)

    def test_workflow_and_main_gate(self):
        check_workflow_gate_blocks_retries_and_other_lanes()

    def test_canary_checkpoint_publication_and_protection(self):
        for behavior in ('preview', 'apply', 'new-source', 'wrong-mime', 'source-drift',
                         'unrelated-route', 'readback-failure', 'active-redirects'):
            with self.subTest(behavior=behavior):
                self.temp_check(check_canary_checkpoint_publication_and_protection, behavior)
