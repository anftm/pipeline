import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.reader_bucket_store import S3BucketStore
from scripts.reader_gc_cache import ReadCache, RequestPacer
from scripts import reader_gc_documents as gc, reader_document_reset as reset


class CacheTests(unittest.TestCase):
    def test_fresh_exact_version_listing_is_scoped_and_fails_closed(self):
        store=S3BucketStore.__new__(S3BucketStore)
        store.list_workers=2
        store._location=Mock(return_value=('namespace','bucket'))
        values={'chapters/a/index.json':'"version-a"','chapters/b/index.json':'"version-b"',
                'chapters/other/index.json':'"ignored"'}
        class Paginator:
            def paginate(self, Bucket, Prefix):
                return [{'Contents':[{'Key':path,'ETag':etag} for path,etag in values.items()
                                      if path.startswith(Prefix)]}]
        client=Mock();client.get_paginator.return_value=Paginator();store._client=Mock(return_value=client)
        paths={'chapters/a/index.json','chapters/b/index.json'}
        self.assertEqual(store.list_exact_versions('bucket',paths),{p:values[p] for p in paths})
        values['chapters/b/index.json']='W/"weak"'
        with self.assertRaisesRegex(ValueError,'strong'):
            store.list_exact_versions('bucket',paths)
        values.pop('chapters/b/index.json')
        with self.assertRaisesRegex(ValueError,'disappeared'):
            store.list_exact_versions('bucket',paths)

    def test_exact_version_listing_includes_files_directly_under_root_directory(self):
        store=S3BucketStore.__new__(S3BucketStore);store.list_workers=2
        store._location=Mock(return_value=('namespace','bucket'))
        paths={'reader-index/reader_assets.json.gz','reader-index/reader_catalog.json',
               'reader-index/processing/owner.json','current.json'}
        requested=[]
        class Paginator:
            def paginate(self,Bucket,Prefix):
                requested.append(Prefix)
                return [{'Contents':[{'Key':p,'ETag':'"v"'} for p in paths if p.startswith(Prefix)]}]
        client=Mock();client.get_paginator.return_value=Paginator();store._client=Mock(return_value=client)
        self.assertEqual(set(store.list_exact_versions('bucket',paths)),paths)
        self.assertIn('reader-index/',requested)
        self.assertNotIn('reader-index/reader_catalog.json/',requested)

    def test_exact_version_listing_rejects_conflicting_duplicate_page_entries(self):
        store=S3BucketStore.__new__(S3BucketStore);store.list_workers=1
        store._location=Mock(return_value=('namespace','bucket'))
        paths={'reader-index/processing/a.json','reader-index/processing/b.json'}
        class Paginator:
            def paginate(self,Bucket,Prefix):
                yield {'Contents':[{'Key':'reader-index/processing/a.json','ETag':'"v1"'}]}
                yield {'Contents':[{'Key':'reader-index/processing/a.json','ETag':'"v2"'},
                                   {'Key':'reader-index/processing/b.json','ETag':'"v1"'}]}
        client=Mock();client.get_paginator.return_value=Paginator();store._client=Mock(return_value=client)
        with self.assertRaisesRegex(ValueError,'conflicting S3 versions'):
            store.list_exact_versions('bucket',paths)

    def test_missing_read_time_etag_forgets_an_older_get(self):
        store=S3BucketStore.__new__(S3BucketStore)
        store._remember_etag('b','p',{'ETag':'"old"'})
        store._remember_etag('b','p',{})
        self.assertNotIn(('b','p'),store.read_etags)

    def test_exact_version_listing_shards_ebook_hashes_not_each_source(self):
        store=S3BucketStore.__new__(S3BucketStore);store.list_workers=4
        store._location=Mock(return_value=('namespace','bucket'))
        paths={f'chapters/ebook/chm/{source}/aaaaaaaaaaaaaaaa/chapter-manifest.json' for source in
               ('01'+'a'*62,'01'+'b'*62,'1f'+'c'*62,'ff'+'d'*62)}
        prefixes=[]
        class Paginator:
            def paginate(self, Bucket, Prefix):
                prefixes.append(Prefix)
                return [{'Contents':[{'Key':path,'ETag':'"v"'} for path in paths if path.startswith(Prefix)]}]
        client=Mock();client.get_paginator.return_value=Paginator();store._client=Mock(return_value=client)
        self.assertEqual(set(store.list_exact_versions('bucket',paths)),paths)
        self.assertEqual(set(prefixes),{'chapters/ebook/chm/01','chapters/ebook/chm/1f','chapters/ebook/chm/ff'})

    def test_exact_version_listing_does_not_combine_pages_across_retries(self):
        from botocore.exceptions import EndpointConnectionError
        store=S3BucketStore.__new__(S3BucketStore);store.list_workers=1
        store._location=Mock(return_value=('namespace','bucket'))
        paths={'reader-index/processing/a.json','reader-index/processing/b.json'}
        attempts=[0]
        class Paginator:
            def paginate(self, Bucket, Prefix):
                attempts[0]+=1
                if attempts[0]==1:
                    yield {'Contents':[{'Key':'reader-index/processing/a.json','ETag':'"old"'}]}
                    raise EndpointConnectionError(endpoint_url='https://fixture.invalid')
                yield {'Contents':[{'Key':'reader-index/processing/b.json','ETag':'"new"'}]}
        client=Mock();client.get_paginator.return_value=Paginator();store._client=Mock(return_value=client)
        with patch('scripts.reader_bucket_store.time.sleep'):
            with self.assertRaisesRegex(ValueError,'disappeared'):
                store.list_exact_versions('bucket',paths)
        self.assertEqual(attempts[0],2)

    def test_persisted_cache_needs_current_etag_and_regular_reads_stay_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.sqlite3'
            cache = ReadCache(path)
            raw = b'{"kind":"original"}'
            digest = hashlib.sha256(raw).hexdigest()
            cache.put('bucket', 'reader-index/a.json', 'raw-v1', 'etag1', raw, digest)
            cache.close()
            cache = ReadCache(path)
            self.addCleanup(cache.close)
            store = S3BucketStore.__new__(S3BucketStore)
            store.read_cache = cache
            store.object_etags = {'bucket': {'reader-index/a.json': 'etag1'}}
            store._location = Mock(return_value=('namespace', 'bucket'))
            client = Mock()
            client.get_object.return_value = {'Body': io.BytesIO(b'{"kind":"changed"}'), 'ETag': 'etag2',
                                              'ContentLength':len(b'{"kind":"changed"}')}
            store._client = Mock(return_value=client)
            self.assertEqual(store.read_cached_bytes('bucket', 'reader-index/a.json'), raw)
            client.get_object.assert_not_called()
            self.assertEqual(store.read_bytes('bucket', 'reader-index/a.json'), b'{"kind":"changed"}')
            self.assertIsNone(cache.get('bucket', 'reader-index/a.json', 'raw-v1', 'etag1'))
            self.assertIsNone(cache.get('bucket', 'reader-index/a.json', 'raw-v1', None))

    def test_corrupt_cache_is_a_miss_and_graph_cache_preserves_source_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ReadCache(Path(directory) / 'cache.sqlite3')
            self.addCleanup(cache.close)
            cache.put('b', 'p', 'graph-json-v1', 'e', b'{"resources":[]}', 'a' * 64)
            self.assertEqual(cache.get('b', 'p', 'graph-json-v1', 'e')[1], 'a' * 64)
            cache.db.execute('UPDATE reads SET packed=?', (b'corrupt',))
            self.assertIsNone(cache.get('b', 'p', 'graph-json-v1', 'e'))

    def test_truncated_s3_object_is_never_accepted_as_complete_evidence(self):
        store=S3BucketStore.__new__(S3BucketStore);store.read_cache=None
        store._location=Mock(return_value=('namespace','bucket'))
        client=Mock();client.get_object.side_effect=lambda **_: {
            'Body':io.BytesIO(b'{}'),'ETag':'"fixture"','ContentLength':4}
        store._client=Mock(return_value=client)
        with patch('scripts.reader_bucket_store.time.sleep'):
            for read in (lambda:store.read_bytes('bucket','reader-index/test.json'),
                         lambda:store.read_graph_json('bucket','reader-index/test.json'),
                         lambda:store.digest('bucket','reader-index/test.json')):
                with self.subTest(reader=read), self.assertRaisesRegex(OSError,'short Reader S3'):
                    read()

    def test_s3_sha256_evidence_requires_an_exact_nonnegative_content_length(self):
        store=S3BucketStore.__new__(S3BucketStore)
        store._location=Mock(return_value=('namespace','bucket'))
        client=Mock();store._client=Mock(return_value=client)
        for length in (None, -1, True, '2'):
            client.get_object.side_effect=lambda **_: {'Body':io.BytesIO(b'{}'),'ContentLength':length}
            for method in (store.read_bytes,store.read_graph_json,store.digest):
                with self.subTest(length=length,method=method.__name__), \
                     self.assertRaisesRegex(ValueError,'no exact content length'):
                    method('bucket','reader-index/test.json')

    def test_graph_parser_never_reuses_old_cache_with_omitted_nested_references(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ReadCache(Path(directory) / 'cache.sqlite3')
            self.addCleanup(cache.close)
            path = 'reader-index/a.json'
            raw = json.dumps({'files': {'book': {'text': [
                {'bucket': 'vomebook/ccrd-index', 'path': 'text/objects/x.txt'}]}}}).encode()
            digest = hashlib.sha256(raw).hexdigest()
            cache.put('bucket', path, 'graph-json-v1', 'etag1', b'{"files":{"book":{}}}', digest)
            store = S3BucketStore.__new__(S3BucketStore)
            store.read_cache = cache
            store.object_etags = {'bucket': {path: 'etag1'}}
            store._location = Mock(return_value=('namespace', 'bucket'))
            client = Mock()
            client.get_object.return_value = {'Body': io.BytesIO(raw), 'ETag': 'etag1', 'ContentLength':len(raw)}
            store._client = Mock(return_value=client)
            parsed, observed = store.read_graph_json('bucket', path)
            self.assertEqual(parsed['files']['book']['text'][0]['path'], 'text/objects/x.txt')
            self.assertEqual(observed, digest)
            client.get_object.assert_called_once()
            parsed_again, observed_again = store.read_graph_json('bucket', path)
            self.assertEqual((parsed_again, observed_again), (parsed, digest))
            client.get_object.assert_called_once()

    def test_pacer_throttles_every_attempt_and_honors_shared_cooldown(self):
        now = [0.0]
        sleeps = []
        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay
        pacer = RequestPacer(4, clock=lambda: now[0], sleep=sleep)
        pacer.before_send()
        pacer.before_send()
        self.assertEqual(now[0], .25)
        pacer.after_response(response=(SimpleNamespace(status_code=503, headers={'retry-after': '45'}),
                                       {'Error': {'Code': 'SlowDown'}}))
        pacer.before_send()
        self.assertEqual(now[0], 45.25)
        self.assertLessEqual(max(sleeps), 30)
        self.assertEqual(pacer.interval, .5)

    def test_processing_scan_resumes_both_selected_and_retained_records_by_version(self):
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        paths = [f'reader-index/processing/{n}.json' for n in range(3)]
        values = {p: {'kind': 'reader-processing-roots', 'resources': [
            {'bucket': reset.PAGES if i != 1 else reset.ASSETS,
             'root': root if i != 1 else 'pages/image/tif/a'}]} for i, p in enumerate(paths)}
        store = SimpleNamespace(object_etags={reset.ASSETS: {p: 'etag1' for p in paths}}, read_etags={})
        reads = []
        fail = [True]
        def read(bucket, path):
            reads.append(path)
            if path == paths[2] and fail[0]:
                raise OSError('connection lost')
            store.read_etags[bucket, path] = store.object_etags[bucket][path]
            return reset.encode(values[path])
        store.read_bytes = read
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(OSError, 'connection lost'):
                gc.scan_processing(store, set(paths), {'reset_id': 'test-reset'}, workspace)
            fail[0] = False
            reads.clear()
            selected, _ = gc.scan_processing(store, set(paths), {'reset_id': 'test-reset'}, workspace)
            self.assertEqual(reads, [paths[2]])
            self.assertEqual(selected, {paths[0], paths[2]})
            store.object_etags[reset.ASSETS][paths[0]] = 'etag2'
            values[paths[0]] = values[paths[1]]
            reads.clear()
            selected, _ = gc.scan_processing(store, set(paths), {'reset_id': 'test-reset'}, workspace)
            self.assertEqual(reads, [paths[0]])
            self.assertEqual(selected, {paths[2]})

    def test_failed_audit_recovery_excludes_rewritten_and_missing_objects(self):
        import gzip
        import zipfile
        from datetime import datetime, timezone
        paths = [f'reader-index/processing/{n}.json' for n in range(3)]
        rows = [{'path': path, 'sha256': 'a' * 64, 'bytes': 100, 'resources': [
            {'bucket': reset.PAGES, 'root': 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16}]} for path in paths]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('collection.json', json.dumps({'accepted_objects': 0, 'complete': False}))
            z.writestr('processing-retirement.jsonl.gz', gzip.compress(
                '\n'.join(json.dumps(row) for row in rows).encode()))
        replies = [Mock(json=lambda: {'status': 'completed', 'conclusion': 'failure',
                   'display_title': 'Reader GC test-reset', 'path': '.github/workflows/reader-gc-collect.yml',
                   'run_started_at': '2026-10-25T08:00:00Z'}),
                   Mock(json=lambda: {'artifacts': [{'name': 'reader-gc-collection', 'expired': False,
                          'size_in_bytes': 1000, 'archive_download_url': 'https://api.github.com/archive'}]}),
                   Mock(content=archive.getvalue())]
        store = SimpleNamespace(object_etags={reset.ASSETS: dict.fromkeys(paths, 'etag')},
            object_modified={(reset.ASSETS, paths[0]): datetime(2026, 10, 24, tzinfo=timezone.utc),
                             (reset.ASSETS, paths[1]): datetime(2026, 10, 25, 9, tzinfo=timezone.utc)},
            object_sizes={(reset.ASSETS, p): 100 for p in paths})
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': 'anftm/pipeline', 'GH_TOKEN': 'mock'}), \
                patch('httpx.get', side_effect=replies):
            recovered = gc.recover_processing_audit(store, set(paths), {'reset_id': 'test-reset'}, '123')
        self.assertEqual([row['path'] for row in recovered], [paths[0]])
