import io
import json
import unittest
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.reader_bucket_store import HubBucketStore, S3BucketStore


class ReaderBucketStoreTests(unittest.TestCase):
    def test_s3_collection_enforces_bucket_and_key_boundaries_before_deleting(self):
        from scripts.reader_gc_store import CollectionStore, CcrdCollectionStore
        for cls, allowed, forbidden in [(CollectionStore, 'vomebook/reader-assets-v2', 'vomebook/ccrd-index'),
                                        (CcrdCollectionStore, 'vomebook/ccrd-index', 'vomebook/reader-assets-v2')]:
            store = cls.__new__(cls)
            with patch.object(S3BucketStore, 'delete') as delete:
                for bucket, paths in [(forbidden, ['objects/old']), (allowed, ['../source']),
                                      (allowed, ['objects//bad']), ('upstream/source', ['old'])]:
                    with self.assertRaises(ValueError):
                        store.delete(bucket, paths)
                delete.assert_not_called()
                store.delete(allowed, ['objects/old'])
                delete.assert_called_once_with(allowed, ['objects/old'])

    def test_delete_retries_only_the_current_bounded_hub_batch(self):
        paths = [f'objects/retired/{i}.bin' for i in range(1201)]
        calls = []
        def remove(bucket, *, delete, token):
            calls.append(list(delete))
            if len(calls) == 2:
                raise ConnectionError('429 Too Many Requests')
        with patch('huggingface_hub.HfApi'), \
                patch('huggingface_hub.batch_bucket_files', side_effect=remove), \
                patch('scripts.shared.time.sleep'):
            HubBucketStore().delete('vomebook/reader-assets-v2', paths)
        self.assertEqual(calls, [paths[:500], paths[500:1000], paths[500:1000], paths[1000:]])

    def test_hub_put_bytes_retries_connection_style_xet_throttling(self):
        error = ConnectionError(
            "Network error: Request error: HTTP status client error "
            "(429 Too Many Requests), domain: "
            "https://huggingface.co/api/buckets/vomebook/ccrd-index/xet-write-token")
        error.response = Mock(status_code=429, headers={"Retry-After": "4"})
        payload = b"complete immutable OCR text"
        with patch.dict("os.environ", {"HF_TOKEN": "ccrd-token"}), \
                patch("huggingface_hub.HfApi"), \
                patch("huggingface_hub.batch_bucket_files", side_effect=[error, None]) as upload, \
                patch("scripts.shared.time.sleep") as sleep, \
                patch("scripts.shared.random.uniform", return_value=0):
            HubBucketStore().put_bytes("vomebook/ccrd-index", "text/objects/test.txt", payload)
        self.assertEqual(upload.call_count, 2)
        for call in upload.call_args_list:
            self.assertEqual(call.args, ("vomebook/ccrd-index",))
            self.assertEqual(call.kwargs, {
                "add": [(payload, "text/objects/test.txt")], "token": "ccrd-token"})
        sleep.assert_called_once_with(4)

    def test_hub_put_json_retries_the_same_staged_bytes(self):
        error = ConnectionError("429 Too Many Requests at xet-write-token")
        uploaded = []

        def upload(bucket, *, add, token):
            local, remote = add[0]
            uploaded.append((bucket, remote, Path(local).read_bytes(), token, local))
            if len(uploaded) == 1:
                raise error

        with patch.dict("os.environ", {"HF_TOKEN": "ccrd-token"}), \
                patch("huggingface_hub.HfApi"), \
                patch("huggingface_hub.batch_bucket_files", side_effect=upload), \
                patch("scripts.shared.time.sleep"), \
                patch("scripts.shared.random.uniform", return_value=0):
            HubBucketStore().put_json("vomebook/ccrd-index", "control/test.json", {"complete": True})
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(uploaded[0], uploaded[1])
        self.assertEqual(uploaded[0][2], b'{"complete": true}\n')
        self.assertFalse(Path(uploaded[0][4]).exists())

    def test_hub_upload_retries_are_bounded_and_do_not_hide_integrity_errors(self):
        throttled = ConnectionError("429 Too Many Requests at xet-write-token")
        with patch("huggingface_hub.HfApi"), \
                patch("huggingface_hub.batch_bucket_files", side_effect=throttled) as upload, \
                patch("scripts.shared.time.sleep") as sleep:
            with self.assertRaises(ConnectionError):
                HubBucketStore().put_bytes("vomebook/ccrd-index", "text/objects/test.txt", b"same")
        self.assertEqual(upload.call_count, 12)
        self.assertEqual(sleep.call_count, 11)
        with patch("huggingface_hub.HfApi"), \
                patch("huggingface_hub.batch_bucket_files", side_effect=ValueError("invalid payload")) as upload, \
                patch("scripts.shared.time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "invalid payload"):
                HubBucketStore().put_bytes("vomebook/ccrd-index", "text/objects/test.txt", b"same")
        upload.assert_called_once()
        sleep.assert_not_called()

    def test_root_digest_retries_partial_reads_from_a_fresh_hash(self):
        import hashlib
        import httpx
        from contextlib import contextmanager
        payload = b'complete root'
        response = Mock()
        response.status_code = 200
        calls = 0
        @contextmanager
        def stream(*args, **kwargs):
            nonlocal calls
            calls += 1
            def chunks():
                if calls == 1:
                    yield b'partial'
                    raise httpx.RemoteProtocolError('connection interrupted')
                yield payload
            response.iter_bytes.side_effect = chunks
            yield response
        session = Mock()
        session.stream.side_effect = stream
        with patch('huggingface_hub.HfApi'), patch('huggingface_hub.utils.get_session', return_value=session), \
                patch('huggingface_hub.utils.hf_raise_for_status'), patch('scripts.reader_bucket_store.time.sleep'):
            store = HubBucketStore()
            store.pace_resolver = Mock()
            self.assertEqual(store.digest('vomebook/reader-assets-v2', 'reader-index/root.json'),
                             hashlib.sha256(payload).hexdigest())
        self.assertEqual(calls, 2)

    def test_hub_read_bytes_retries_resolver_429_and_honors_retry_after(self):
        import httpx
        request = httpx.Request("GET", "https://huggingface.co/buckets/bucket/resolve/path")
        throttled = httpx.Response(429, headers={"Retry-After": "300"}, request=request)
        success = httpx.Response(200, content=b"root", request=request)
        session = Mock()
        session.get.side_effect = [throttled, success]
        sleeps = []
        with patch("huggingface_hub.HfApi"), \
                patch("huggingface_hub.utils.get_session", return_value=session), \
                patch("scripts.reader_bucket_store.time.sleep", side_effect=sleeps.append):
            store = HubBucketStore()
            store.pace_resolver = Mock()
            self.assertEqual(store.read_bytes("vomebook/reader-assets-v2", "reader-index/current.json"), b"root")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(store.pace_resolver.call_count, 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 300)
        self.assertLess(sleeps[0], 302)

    def test_hub_read_bytes_honors_retry_countdown_in_hf_error_body(self):
        import httpx
        request = httpx.Request("GET", "https://huggingface.co/buckets/bucket/resolve/path")
        throttled = httpx.Response(
            429, content=b"429 Too Many Requests. Retry after 23 seconds (0/5000 requests remaining).",
            request=request)
        success = httpx.Response(200, content=b"root", request=request)
        session = Mock()
        session.get.side_effect = [throttled, success]
        sleeps = []
        with patch("huggingface_hub.HfApi"), \
                patch("huggingface_hub.utils.get_session", return_value=session), \
                patch("scripts.reader_bucket_store.time.sleep", side_effect=sleeps.append), \
                patch("scripts.reader_bucket_store.random.uniform", return_value=0.5):
            store = HubBucketStore()
            store.pace_resolver = Mock()
            self.assertEqual(store.read_bytes("vomebook/reader-assets-v2", "reader-index/current.json"), b"root")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(sleeps, [23.5])

    def test_legacy_environment_cannot_remap_the_current_input_bucket(self):
        result = subprocess.check_output(["python3", "-B", "-c",
            "from scripts.shared import PDF_OCR_INPUT_BUCKET; print(PDF_OCR_INPUT_BUCKET)"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PDF_OCR_INPUT_BUCKET": "pdf-jxl"}, text=True)
        self.assertEqual(result.strip(), "melsm/pdf-archive-v2")

    def test_hub_inventory_uses_separate_input_credentials(self):
        from types import SimpleNamespace
        with patch.dict("os.environ", {"HF_TOKEN": "reader-token", "HF_INPUT_TOKEN": "input-token"}), \
                patch("huggingface_hub.HfApi") as api:
            api.return_value.list_bucket_tree.return_value = [SimpleNamespace(type="file", path="objects/page.png")]
            store = HubBucketStore()
            self.assertEqual(store.list_files("melsm/pdf-archive-v2", ("",)), {"objects/page.png"})
            self.assertEqual(api.return_value.list_bucket_tree.call_args.kwargs["token"], "input-token")
            store.list_files("vomebook/pdf-pages-v2", ("",))
            self.assertEqual(api.return_value.list_bucket_tree.call_args.kwargs["token"], "reader-token")

    def store(self, client):
        store = S3BucketStore.__new__(S3BucketStore)
        store._location = Mock(return_value=("vomebook", "reader-assets-v2"))
        store._client = Mock(return_value=client)
        return store

    def test_head_inventory_distinguishes_missing_from_permissions(self):
        class Missing(Exception):
            response = {"Error": {"Code": "404"}}
        class Denied(Exception):
            response = {"Error": {"Code": "AccessDenied"}}
        client = Mock()
        client.head_object.side_effect = [None, Missing()]
        store = self.store(client)
        self.assertEqual(store.existing_files("bucket", {"a", "b"}), {"a"})
        client.head_object.side_effect = Denied()
        with self.assertRaises(Denied):
            store.existing_files("bucket", {"a"})

    def test_read_closes_the_response_body(self):
        body = io.BytesIO(b"json")
        client = Mock()
        client.get_object.return_value = {"Body": body, "ContentLength": 4}
        self.assertEqual(self.store(client).read_bytes("bucket", "path"), b"json")
        self.assertTrue(body.closed)

    def test_s3_graph_read_retries_interrupted_streams_but_not_missing_objects(self):
        from botocore.exceptions import ClientError, ResponseStreamingError
        from scripts.reader_bucket_store import S3NotFound
        class Broken(io.BytesIO):
            def read(self, size=-1):
                raise ResponseStreamingError(error="connection reset")
        client = Mock()
        client.get_object.side_effect = [{"Body": Broken()}, {"Body": io.BytesIO(b'{"files": {}}'),
                                         "ContentLength": len(b'{"files": {}}'), "ETag": '"e"'}]
        store = self.store(client)
        with patch("scripts.reader_bucket_store.time.sleep"):
            payload, _digest = store.read_graph_json("vomebook/reader-assets-v2", "reader-index/root.json")
        self.assertEqual(payload, {"files": {}})
        self.assertEqual(client.get_object.call_count, 2)
        self.assertEqual(store.read_etags[("vomebook/reader-assets-v2", "reader-index/root.json")], '"e"')
        client.get_object.reset_mock()
        client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"},
                                                     "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
        with self.assertRaises(S3NotFound):
            store.read_bytes("vomebook/reader-assets-v2", "missing.json")
        client.get_object.assert_called_once()

    def test_missing_bucket_is_never_reported_as_a_missing_key(self):
        from botocore.exceptions import ClientError
        error=ClientError({'Error':{'Code':'NoSuchBucket'},
                           'ResponseMetadata':{'HTTPStatusCode':404}},'GetObject')
        client=Mock();client.get_object.side_effect=error;client.head_object.side_effect=error
        store=self.store(client)
        with self.assertRaises(ClientError):
            store.read_bytes('bucket','path')
        with self.assertRaises(ClientError):
            store.digest('bucket','path')
        with self.assertRaises(ClientError):
            store.existing_files('bucket',{'path'})

    def test_s3_inventory_shards_prefixes_and_records_root_etags(self):
        pages = {
            ("", "/"): {"Contents": [{"Key": "root.json", "ETag": '"r"', "Size": 2}],
                        "CommonPrefixes": [{"Prefix": "objects/"}]},
            ("objects/", "/"): {"CommonPrefixes": [{"Prefix": f"objects/{i:02x}/"} for i in range(16)]},
        }
        def paginate(Bucket, Prefix, Delimiter=None):
            if (Prefix, Delimiter) in pages:
                return [pages[(Prefix, Delimiter)]]
            return [{"Contents": [{"Key": Prefix + "index.json", "ETag": '"' + Prefix + '"', "Size": 1}]}]
        client = Mock()
        client.get_paginator.return_value.paginate.side_effect = paginate
        store = self.store(client)
        store.root_predicate = lambda path: path.endswith(".json")
        files = store.list_files("vomebook/reader-assets-v2", ("",))
        self.assertEqual(len(files), 17)
        self.assertEqual(store.root_etags["vomebook/reader-assets-v2"]["objects/0a/index.json"], '"objects/0a/"')

    def test_control_inventory_batches_tiny_directories_and_retains_current_etags(self):
        client = Mock()
        calls = []
        def paginate(Bucket, Prefix, **options):
            calls.append((Prefix, options))
            self.assertNotIn('Delimiter', options)
            for start in (0, 1000, 2000):
                yield {'Contents': [{'Key': f'{Prefix}generations/{i:032x}/catalog.json',
                                     'ETag': f'"{i}"', 'Size': 123} for i in range(start, start + 1000)]}
        client.get_paginator.return_value.paginate.side_effect = paginate
        store = self.store(client)
        store.root_predicate = lambda path: True
        files = store.list_files('vomebook/reader-assets-v2', ('reader-index/',))
        self.assertEqual(len(files), 3000)
        self.assertEqual(calls, [('reader-index/', {})])
        last = 'reader-index/generations/' + f'{2999:032x}' + '/catalog.json'
        self.assertEqual(store.root_etags['vomebook/reader-assets-v2'][last], '"2999"')
        self.assertEqual(store.object_sizes['vomebook/reader-assets-v2', last], 123)

    def test_deep_and_wide_inventories_fall_back_without_dropping_objects(self):
        for count, prefix in [(3000, 'chapters/ebook/'), (250, 'objects/aa/')]:
            with self.subTest(prefix=prefix):
                entries = {prefix + f'{i:064x}/page.png': {'Key': prefix + f'{i:064x}/page.png',
                            'Size': i + 1, 'ETag': f'"{i}"'} for i in range(count)}
                entries['root.json'] = {'Key': 'root.json', 'Size': 10}
                calls = []
                def paginate(Bucket, Prefix, Delimiter=None):
                    calls.append((Prefix, Delimiter))
                    matched = {key: value for key, value in entries.items() if key.startswith(Prefix)}
                    if not Delimiter:
                        for start in range(0, len(matched), 1000):
                            yield {'Contents': list(matched.values())[start:start + 1000]}
                        return
                    children = sorted({Prefix + key[len(Prefix):].split('/')[0] + '/'
                                       for key in matched if '/' in key[len(Prefix):]})
                    yield {'CommonPrefixes': [{'Prefix': child} for child in children[:1000]],
                           'Contents': [v for key, v in matched.items() if '/' not in key[len(Prefix):]]}
                    if len(children) > 1000:
                        yield {'CommonPrefixes': [{'Prefix': child} for child in children[1000:]]}
                client = Mock()
                client.get_paginator.return_value.paginate.side_effect = paginate
                store = self.store(client)
                self.assertEqual(store.list_files('bucket', ('',)), set(entries))
                self.assertLessEqual(len(calls), 4)
                self.assertEqual(store.object_sizes['bucket', prefix + f'{count - 1:064x}/page.png'], count)

    def test_wide_prefix_discovery_stops_before_listing_thousands_of_directories(self):
        client = Mock()
        def paginate(**kwargs):
            yield {'CommonPrefixes': [{'Prefix': f'objects/{i}/'} for i in range(1000)]}
            raise AssertionError('wide delimiter discovery should stop after the first page')
        client.get_paginator.return_value.paginate.side_effect = paginate
        self.assertEqual(self.store(client)._shards(client, 'bucket', 'objects/'), ({}, ['objects/']))

    def test_s3_root_listing_reads_index_dirs_without_full_inventory(self):
        calls = []
        def paginate(Bucket, Prefix, Delimiter=None):
            calls.append((Prefix, Delimiter))
            if Prefix == "reader-index/" and Delimiter:
                return [{"Contents": [{"Key": "reader-index/catalog.json", "ETag": '"c"'}],
                         "CommonPrefixes": [{"Prefix": "reader-index/processing/"}]}]
            if Prefix == "reader-index/processing/":
                return [{"Contents": [{"Key": "reader-index/processing/a.json", "ETag": '"a"'}]}]
            if Prefix == "chapters/ebook/epub/":
                return [{"Contents": [{"Key": "chapters/ebook/epub/index.json", "ETag": '"e"'},
                                      {"Key": "chapters/ebook/epub/readme.txt", "ETag": '"t"'}]}]
            raise AssertionError(Prefix)
        client = Mock()
        client.get_paginator.return_value.paginate.side_effect = paginate
        store = self.store(client)
        store.root_predicate = lambda path: path.endswith(".json")
        roots = store.list_roots("vomebook/reader-assets-v2", {"chapters/ebook/epub/"})
        self.assertEqual(roots, {"reader-index/catalog.json": '"c"', "reader-index/processing/a.json": '"a"',
                                 "chapters/ebook/epub/index.json": '"e"'})
        self.assertIn(("chapters/ebook/epub/", "/"), calls)
        self.assertNotIn(("", None), calls)

    def test_delete_checks_per_object_errors(self):
        client = Mock()
        client.delete_objects.return_value = {"Errors": [{"Key": "a", "Code": "AccessDenied"}]}
        with self.assertRaises(RuntimeError):
            self.store(client).delete("bucket", ["a"])

    def test_s3_delete_retries_partial_transient_errors_without_replaying_other_batches(self):
        from botocore.exceptions import ClientError
        client = Mock()
        client.delete_objects.side_effect = [{}, {'Errors': [{'Key': '1001', 'Code': 'SlowDown'}]},
                                             ClientError({'Error': {'Code': '503'}}, 'DeleteObjects'), {}]
        paths = [str(i) for i in range(1003)]
        with patch('scripts.reader_bucket_store.time.sleep'):
            self.store(client).delete('bucket', paths)
        batches = [[row['Key'] for row in call.kwargs['Delete']['Objects']]
                   for call in client.delete_objects.call_args_list]
        self.assertEqual(batches, [paths[:1000]] + [paths[1000:]] * 3)

    def test_integrity_scan_preserves_page_and_chapter_contracts(self):
        store = self.store(Mock())
        manifests = {
            "objects/a/page-manifest.json": {"page_count": 2},
            "ebook-chapters/objects/b/chapter-manifest.json": {
                "chapters": [{"path": "chapters/chapter-0001.xhtml"}],
                "search_index": {"path": "epub-search-index.json.gz"},
            },
        }
        store.read_bytes = Mock(side_effect=lambda bucket, path: json.dumps(manifests[path]).encode())
        self.assertEqual(store.manifest_references("bucket", set(manifests)), {
            "objects/a/page-manifest.json": {
                "objects/a/pages/page-000001.webp", "objects/a/pages/page-000002.webp"},
            "ebook-chapters/objects/b/chapter-manifest.json": {
                "ebook-chapters/objects/b/chapters/chapter-0001.xhtml",
                "ebook-chapters/objects/b/epub-search-index.json.gz"},
        })


if __name__ == "__main__":
    unittest.main()
