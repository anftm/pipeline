import io
import json
import unittest
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.reader_bucket_store import HubBucketStore, S3BucketStore


class ReaderBucketStoreTests(unittest.TestCase):
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
        client.get_object.return_value = {"Body": body}
        self.assertEqual(self.store(client).read_bytes("bucket", "path"), b"json")
        self.assertTrue(body.closed)

    def test_s3_graph_read_retries_interrupted_streams_but_not_missing_objects(self):
        from botocore.exceptions import ClientError, ResponseStreamingError
        from scripts.reader_bucket_store import S3NotFound
        class Broken(io.BytesIO):
            def read(self, size=-1):
                raise ResponseStreamingError(error="connection reset")
        client = Mock()
        client.get_object.side_effect = [{"Body": Broken()}, {"Body": io.BytesIO(b'{"files": {}}'), "ETag": '"e"'}]
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
