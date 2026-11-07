import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from scripts import pilot_image_page_stream as stream, finalize_image_index as finalizer
from scripts import reader_stream_index as index


class ImageAssetStreamTests(unittest.TestCase):
    def test_unchanged_lfs_source_is_reused_but_changed_and_unknown_sources_are_built(self):
        rows = [{"repo": "repo", "path": name + ".jpg", "extension": "jpg", "bytes": 10,
                 "revision": "new"} for name in ("unchanged", "changed", "unknown")]
        previous = {"files": [{"key": f"repo\0{name}.jpg", "source_revision": "old",
                               "source_sha256": digest, "path": "pages/image/jpg/" + digest + "/document.webp"}
                              for name, digest in (("unchanged", "a" * 64), ("changed", "b" * 64),
                                                   ("unknown", "c" * 64))]}
        remote = [SimpleNamespace(path="unchanged.jpg", lfs=SimpleNamespace(sha256="a" * 64)),
                  SimpleNamespace(path="changed.jpg", lfs=SimpleNamespace(sha256="d" * 64)),
                  SimpleNamespace(path="unknown.jpg", lfs=None)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revisions = root / "revisions.json"
            revisions.write_text('{}')
            args = SimpleNamespace(limit=0, max_source_bytes=0, shard_count=1, shard_index=0,
                                   revisions=revisions, extension="all", bucket=stream.TARGET_BUCKET, retry_failures=False,
                                   search_data=root / "search.json", apply=False, strict=True)
            built = []
            def build(item, work, _token, _bucket):
                built.append(item['path'])
                image_root = 'pages/image/jpg/' + 'f' * 64
                return {'key': f"repo\0{item['path']}", 'root': image_root,
                        'source_revision': item['revision'], 'path': image_root + '/document.webp'}, {}
            with patch.object(stream, 'parse_args', return_value=args), \
                 patch.object(stream, 'source_records', return_value=rows), \
                 patch.object(stream, 'existing_index', return_value=previous), \
                 patch('scripts.reader_stream_index.HfApi') as api, \
                 patch.object(stream, 'build_one', side_effect=build):
                api.return_value.get_paths_info.return_value = remote
                self.assertEqual(stream.main(), 0)
            self.assertEqual(built, ['changed.jpg', 'unknown.jpg'])

    def test_manual_workflow_is_bounded_serialized_and_cannot_reset_indexes(self):
        import subprocess
        import yaml
        workflow = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/reader-image-stream.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual(set(triggers), {'workflow_dispatch'})
        inputs = triggers['workflow_dispatch']['inputs']
        self.assertEqual(inputs['limit']['default'], '1')
        self.assertFalse(inputs['apply']['default'])
        self.assertNotIn('rebuild', inputs)
        self.assertTrue(inputs['retry_failures'])
        self.assertEqual(inputs['shard']['options'], ['all', *map(str, range(8))])
        for name in ('build', 'finalize'):
            self.assertEqual(workflow['jobs'][name]['concurrency']['group'], 'reader-sidecar')
        commands = '\n'.join(step.get('run', '') for job in workflow['jobs'].values() for step in job['steps'])
        self.assertNotIn('reset_image_index.py', commands)
        self.assertIn('finalize_image_index.py --apply', commands)
        guard = next(step['run'] for step in workflow['jobs']['plan']['steps'] if step.get('id') == 'matrix').split('args=(', 1)[0]
        for limit, apply, valid in [('0', 'false', True), ('1', 'true', True), ('3', 'true', True),
                                    ('0', 'true', False), ('4', 'true', False), ('-1', 'false', False)]:
            with self.subTest(limit=limit, apply=apply):
                result = subprocess.run(['bash', '-e', '-c', guard], env={'LIMIT': limit, 'APPLY': apply, 'SHARD': 'all'},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, valid)
        result = subprocess.run(['bash', '-e', '-c', guard], env={'LIMIT': '1', 'APPLY': 'true', 'SHARD': '8'},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_index_read_errors_do_not_create_empty_baselines(self):
        with patch.object(stream, 'read_index', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                stream.existing_index('bucket', None, stream.INDEX_NAME)
        with patch.object(stream, 'read_index', return_value={'files': [], 'failures': []}):
            with self.assertRaisesRegex(ValueError, 'canonical image index'):
                stream.existing_index('bucket', None, stream.INDEX_NAME)
            self.assertEqual(stream.existing_index('bucket', None, 'index-v2-00.json')['files'], [])

    def test_image_root_changes_when_rendered_output_changes(self):
        source = 'a' * 64
        self.assertNotEqual(stream.image_root('png', source, {'document.webp': 'b' * 64}),
                            stream.image_root('png', source, {'document.webp': 'c' * 64}))
        self.assertRegex(stream.image_root('png', source, {'document.webp': 'b' * 64}),
                         r'^pages/image/png/[0-9a-f]{64}$')

    def test_images_get_processing_roots_and_content_readback_before_checkpoint(self):
        root = 'pages/image/png/' + 'a' * 64
        entry = {'key': 'repo\0book.png', 'source_revision': 'rev', 'path': root + '/document.webp'}
        writes = []
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            local = work / 'document.webp'
            local.write_bytes(b'webp-fixture')
            def upload(_bucket, files, _token, **_kwargs):
                writes.extend((remote, Path(local).read_bytes()) for local, remote in files)
            with patch.object(index, 'batch_bucket_files_with_retry', side_effect=upload), \
                    patch.object(index, 'remote_identity', side_effect=[None, (hashlib.sha256(local.read_bytes()).hexdigest(), len(local.read_bytes()))]), \
                    patch.object(index, 'HfFileSystem'), \
                    patch.object(index, 'read_index', side_effect=lambda *args: json.loads(writes[-1][1])):
                result = index.publish_checkpoint('vomebook/reader-assets-v2', 'pages/image/index-v2-00.json',
                    {'files': []}, [entry], [], {entry['path']: str(local)}, work, None,
                    apply=True, kind='image-stream-index')
        self.assertTrue(writes[0][0].startswith('reader-index/processing/'))
        self.assertEqual(writes[1][0], entry['path'])
        self.assertEqual(writes[2][0], 'pages/image/index-v2-00.json')
        self.assertTrue(result['files'][0]['processing_roots'])

    def test_finalizer_preserves_canonical_and_rejects_foreign_shards(self):
        key = 'repo\0image.jpg'
        n = index.shard_for(key, 8)
        old = {'key': key, 'source_revision': 'old', 'path': 'old.webp'}
        other = {'key': 'repo\0keep.png', 'path': 'keep.webp'}
        canonical = {'version': 1, 'kind': 'image-stream-index', 'files': [old, other]}
        new = {**old, 'source_revision': 'new', 'path': 'new.webp', 'checkpointed_at': '2026-11-07T12:00:00+00:00'}
        merged = finalizer.merge(canonical, [(n, {'files': [new]})])
        self.assertEqual({e['key']: e for e in merged['files']}, {key: new, other['key']: other})
        self.assertEqual(finalizer.merge(merged, [(n, {'files': [old]})]), merged)
        with self.assertRaisesRegex(ValueError, 'foreign'):
            finalizer.merge(canonical, [((n + 1) % 8, {'files': [new]})])
        with self.assertRaisesRegex(ValueError, 'timestamps'):
            finalizer.merge(merged, [(n, {'files': [{**new, 'path': 'other.webp'}]})])
        with self.assertRaises(ValueError):
            finalizer.merge({'files': {}}, [])
        with self.assertRaisesRegex(ValueError, 'empty'):
            finalizer.merge({'files': []}, [(n, {'files': [new]})])

    def test_image_shard_uses_newer_checkpoint_and_rejects_ambiguous_legacy(self):
        key = 'repo\0image.jpg'; number = index.shard_for(key, 8)
        old = {'key': key, 'path': 'old.webp'}
        new = {'key': key, 'path': 'new.webp', 'checkpointed_at': '2026-11-07T12:00:00+00:00'}
        self.assertEqual(stream.shard_baseline({'files': [new]}, {'files': [old]}, number, 8)['files'], [new])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            stream.shard_baseline({'files': [{**old, 'path': 'other.webp'}]}, {'files': [old]}, number, 8)

    def test_failures_are_checkpointed_with_no_remote_asset_upload_in_preview(self):
        source = {'repo': 'repo', 'path': 'bad.png', 'extension': 'png', 'bytes': 10, 'revision': 'new'}
        previous = {'files': [], 'failures': []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revisions = root / 'revisions.json'
            revisions.write_text('{}')
            args = SimpleNamespace(limit=1, max_source_bytes=0, shard_count=1, shard_index=0,
                revisions=revisions, extension='png', bucket=stream.TARGET_BUCKET,
                retry_failures=True, search_data=root/'search.json', apply=False, strict=True)
            with patch.object(stream, 'parse_args', return_value=args), \
                    patch.object(stream, 'source_records', return_value=[source]), \
                    patch.object(stream, 'existing_index', return_value=previous), \
                    patch.object(stream, 'unchanged_sources', return_value=set()), \
                    patch.object(stream, 'build_one', side_effect=ValueError('broken')) as build, \
                    patch.object(stream, 'publish_checkpoint', wraps=index.publish_checkpoint) as checkpoint, \
                    patch.object(index, 'batch_bucket_files_with_retry') as upload:
                self.assertEqual(stream.main(), 1)
            build.assert_called_once()
            self.assertEqual(checkpoint.call_args.args[4][0]['key'], 'repo\0bad.png')
            self.assertFalse(checkpoint.call_args.kwargs['apply'])
            upload.assert_not_called()

    def test_failed_same_revision_retry_retains_old_route_and_new_failure(self):
        key = 'repo\0image.png'
        old = {'key': key, 'source_revision': 'same', 'path': 'old.webp',
               'checkpointed_at': '2026-11-07T10:00:00+00:00'}
        failure = {'key': key, 'source_revision': 'same', 'error': 'broken',
                   'checkpointed_at': '2026-11-07T11:00:00+00:00'}
        with tempfile.TemporaryDirectory() as directory:
            result = index.publish_checkpoint(stream.TARGET_BUCKET, 'pages/image/index-v2-00.json',
                {'files': [old]}, [], [failure], {}, Path(directory), None,
                apply=False, kind='image-stream-index')
        self.assertEqual(result['files'], [old])
        self.assertEqual(result['failures'], [failure])

    def test_retry_failure_overrides_same_revision_success_in_worker(self):
        source = {'repo': 'repo', 'path': 'retry.png', 'extension': 'png', 'bytes': 10, 'revision': 'same'}
        key = 'repo\0retry.png'
        previous = {'files': [{'key': key, 'source_revision': 'same', 'path': 'old.webp'}],
                    'failures': [{'key': key, 'source_revision': 'same'}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); revisions = root / 'revisions.json'; revisions.write_text('{}')
            args = SimpleNamespace(limit=1, max_source_bytes=0, shard_count=1, shard_index=0,
                revisions=revisions, extension='png', bucket=stream.TARGET_BUCKET, retry_failures=True,
                search_data=root / 'search.json', apply=False, strict=True)
            with patch.object(stream, 'parse_args', return_value=args), \
                    patch.object(stream, 'source_records', return_value=[source]), \
                    patch.object(stream, 'existing_index', return_value=previous), \
                    patch.object(stream, 'unchanged_sources', return_value=set()), \
                    patch.object(stream, 'build_one', side_effect=ValueError('retry observed')) as build:
                self.assertEqual(stream.main(), 1)
            build.assert_called_once()

    def test_gif_is_published_as_one_static_webp_first_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.gif"
            first = Image.new("RGB", (100, 60), "red")
            second = Image.new("RGB", (100, 60), "blue")
            first.save(source, save_all=True, append_images=[second], duration=100, loop=0)
            original = stream.download
            stream.download = lambda _url, target, _token: target.write_bytes(source.read_bytes())
            try:
                entry, uploads = stream.build_one(
                    {"repo": "repo", "path": "book.gif", "extension": "gif",
                     "bytes": source.stat().st_size, "revision": "revision"},
                    root / "work", None, "bucket")
            finally:
                stream.download = original
            self.assertEqual(entry["mode"], "image")
            self.assertEqual(entry["page_count"], 1)
            self.assertEqual(len(uploads), 1)
            remote, local = next(iter(uploads.items()))
            self.assertTrue(remote.endswith("/document.webp"))
            with Image.open(local) as image:
                self.assertEqual(image.n_frames, 1)
                red, green, blue = image.convert("RGB").getpixel((0, 0))
                self.assertGreater(red, blue)

    def test_single_frame_tiff_is_published_as_one_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            Image.new("RGB", (120, 80), "red").save(source, format="TIFF")
            original = stream.download
            stream.download = lambda _url, target, _token: target.write_bytes(source.read_bytes())
            try:
                entry, uploads = stream.build_one(
                    {"repo": "repo", "path": "scan.tif", "extension": "tif",
                     "bytes": source.stat().st_size, "revision": "revision"},
                    root / "work", None, "bucket")
            finally:
                stream.download = original
            self.assertEqual(entry["mode"], "image")
            self.assertEqual(entry["page_count"], 1)
            self.assertTrue(next(iter(uploads)).endswith("/document.webp"))

    def test_multipage_tiff_is_published_as_an_image_page_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            first = Image.new("RGB", (120, 80), "red")
            second = Image.new("RGB", (120, 80), "blue")
            first.save(source, format="TIFF", save_all=True, append_images=[second])
            original = stream.download
            stream.download = lambda _url, target, _token: target.write_bytes(source.read_bytes())
            try:
                entry, uploads = stream.build_one(
                    {"repo": "repo", "path": "scan.tif", "extension": "tif",
                     "bytes": source.stat().st_size, "revision": "revision"},
                    root / "work", None, "bucket")
                manifest = json.loads((root / "work" / "page-manifest.json").read_text())
            finally:
                stream.download = original
            self.assertEqual(entry["mode"], "page-stream")
            self.assertEqual(entry["page_count"], 2)
            self.assertEqual(manifest["kind"], "image-page-stream")
            self.assertEqual([page["path"] for page in manifest["pages"]],
                             ["pages/page-000001.webp", "pages/page-000002.webp"])
            self.assertTrue(next(iter(uploads)).endswith("/pages/page-000001.webp"))


if __name__ == "__main__":
    unittest.main()
