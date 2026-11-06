import asyncio
import io
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app import main, source_files


def source_doc(files=None):
    return {
        "doc_id": "doc-1",
        "title": '标题:/\\*?"<>|',
        "publication_name": "测试刊物",
        "authors": ["作者甲"],
        "content": "正文内容",
        "source_files": files or [],
    }


class FakeStreamResponse:
    def __init__(self, payload=b"source", status_code=200, content_type="application/pdf"):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type, "content-length": str(len(payload)), "accept-ranges": "bytes"}
        self.closed = False

    async def aiter_bytes(self):
        yield self.payload

    async def aclose(self):
        self.closed = True


class DownloadSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_safe_filename_and_content_disposition(self):
        self.assertEqual(source_files.safe_filename(' bad:/\\*?"<>|. ', ".txt"), "bad.txt")
        disposition = source_files.content_disposition("文化 资料.txt")
        self.assertTrue(disposition.startswith("attachment; filename*=UTF-8''"))
        self.assertIn("%E6%96%87%E5%8C%96", disposition)

    def test_source_url_rejects_local_and_private_targets(self):
        for url in (
            "file:///etc/passwd", "http://localhost/x", "http://service.localhost/x",
            "http://127.0.0.1/x", "http://10.0.0.1/x", "http://[::1]/x",
        ):
            self.assertIsNotNone(source_files.validate_source_url(url), url)
        self.assertIsNone(source_files.validate_source_url("https://example.com/file.pdf"))

    def test_dns_resolution_rejects_private_address(self):
        with patch.object(source_files.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            error = asyncio.run(source_files.validate_source_target("https://example.com/file"))
        self.assertIn("disallowed IP", error)

    def test_txt_download_contains_metadata_and_sanitized_filename(self):
        with patch.object(source_files, "get_source_doc", return_value=source_doc()):
            response = self.client.get("/api/download/txt/doc-1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("标题：", response.text)
        self.assertIn("来源：测试刊物", response.text)
        self.assertIn("正文内容", response.text)
        self.assertNotIn("/", response.headers["content-disposition"].split("UTF-8''", 1)[1])

    def test_preview_source_streams_verified_mirror(self):
        upstream = FakeStreamResponse(b"webp bytes", content_type="image/webp")
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.webp"])), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock(return_value=(upstream, True))):
            response = self.client.get("/api/preview/source/doc-1?file=0&inline=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"webp bytes")
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertEqual(response.headers["cache-control"], "public, max-age=86400, s-maxage=86400")
        self.assertEqual(response.headers["x-bha-source-mirror"], "huggingface")
        self.assertTrue(upstream.closed)

    def test_source_preview_bypasses_json_compression_middleware(self):
        payload = b"already-compressed-webp" * 100
        upstream = FakeStreamResponse(payload, content_type="image/webp")
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.webp"])), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock(return_value=(upstream, True))):
            response = self.client.get(
                "/api/preview/source/doc-1?file=0&inline=true",
                headers={"Accept-Encoding": "br, gzip"},
            )
        self.assertEqual(response.content, payload)
        self.assertNotIn("content-encoding", response.headers)

    def test_source_page_streams_requested_mirror_page(self):
        upstream = FakeStreamResponse(b"page webp", content_type="image/webp")
        source = source_doc(["https://raw.githubusercontent.com/o/r/main/a.pdf"])
        with patch.object(source_files, "get_source_doc", return_value=source), \
                patch.object(source_files, "source_mirror_entry", AsyncMock(return_value={"page_previews": {"paths": ["archives0/p1.webp", "archives0/p2.webp"]}})), \
                patch.object(source_files, "open_mirrored_response", AsyncMock(return_value=upstream)):
            response = self.client.get("/api/preview/source-page/doc-1?file=0&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"page webp")
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertEqual(response.headers["cache-control"], "public, max-age=86400, s-maxage=86400")
        self.assertTrue(upstream.closed)

    def test_download_routes_return_stable_missing_errors(self):
        with patch.object(source_files, "get_source_doc", return_value=None):
            for route in ("txt", "source", "source-zip"):
                response = self.client.get(f"/api/download/{route}/missing")
                self.assertEqual(response.status_code, 404, route)

    def test_single_source_stream_preserves_type_and_closes_upstream(self):
        upstream = FakeStreamResponse(b"PDF bytes")
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://example.com/a.pdf"])), \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)):
            response = self.client.get("/api/download/source/doc-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"PDF bytes")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(upstream.closed)

    def test_source_stream_exposes_upstream_length_and_range_support(self):
        upstream = FakeStreamResponse(b"PDF bytes")
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://example.com/a.pdf"])), \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)):
            response = self.client.get("/api/download/source/doc-1?inline=true")
        self.assertEqual(response.headers["content-length"], "9")
        self.assertEqual(response.headers["accept-ranges"], "bytes")

    def test_source_preview_selects_file_and_uses_inline_disposition(self):
        upstream = FakeStreamResponse(b"image bytes", content_type="image/jpeg")
        open_source = AsyncMock(return_value=upstream)
        files = ["https://example.com/1.jpg", "https://example.com/2.jpg"]
        with patch.object(source_files, "get_source_doc", return_value=source_doc(files)), \
                patch.object(source_files, "open_source_response", open_source):
            response = self.client.get("/api/download/source/doc-1?file=1&inline=true")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        open_source.assert_awaited_once_with(files[1])

    def test_source_range_is_forwarded_and_returned(self):
        upstream = FakeStreamResponse(b"partial", status_code=206)
        upstream.headers.update({
            "content-range": "bytes 0-6/100",
            "accept-ranges": "bytes",
            "content-length": "7",
        })
        open_source = AsyncMock(return_value=upstream)
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://example.com/a.pdf"])), \
                patch.object(source_files, "open_source_response", open_source):
            response = self.client.get("/api/download/source/doc-1?inline=true", headers={"Range": "bytes=0-6"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-6/100")
        self.assertEqual(open_source.await_args.args[1], {"Range": "bytes=0-6"})

    def test_source_preview_rejects_active_content(self):
        upstream = FakeStreamResponse(b"<script>alert(1)</script>", content_type="text/html")
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://example.com/file.html"])), \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)):
            response = self.client.get("/api/download/source/doc-1?inline=true")
        self.assertEqual(response.status_code, 415)
        self.assertTrue(upstream.closed)

    def test_source_zip_is_complete_portable_and_cleans_temp_directory(self):
        async def download(_url, target, remaining):
            payload = b"x" * min(32, remaining)
            target.write_bytes(payload)
            return len(payload)

        files = ["https://example.com/a.pdf", "https://example.com/b.jpg"]
        with patch.object(source_files, "get_source_doc", return_value=source_doc(files)), \
                patch.object(source_files, "download_source_to_path", side_effect=download):
            response = self.client.get("/api/download/source-zip/doc-1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            self.assertIsNone(archive.testzip())
            self.assertTrue(all(not any(ch in name for ch in ':*?"<>|\\') for name in archive.namelist()))

    def test_source_zip_enforces_file_and_byte_limits(self):
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://example.com/x"] * (source_files.MAX_SOURCE_FILES + 1))):
            response = self.client.get("/api/download/source-zip/doc-1")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["max_files"], source_files.MAX_SOURCE_FILES)

        async def too_large(_url, _target, _remaining):
            raise ValueError("source files exceed download limit")

        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://example.com/x"])), \
                patch.object(source_files, "download_source_to_path", side_effect=too_large):
            response = self.client.get("/api/download/source-zip/doc-1")
        self.assertEqual(response.status_code, 400)


class SourceMirrorCacheTests(unittest.TestCase):
    def setUp(self):
        self.old_manifest_url = source_files.SOURCE_MIRROR_MANIFEST_URL
        self.old_base_url = source_files.SOURCE_MIRROR_BASE_URL
        source_files.SOURCE_MIRROR_MANIFEST_URL = "https://example.com/manifest.json"
        source_files.SOURCE_MIRROR_BASE_URL = "https://example.com/resolve/main"
        source_files.source_mirror_manifest = None
        source_files.source_mirror_loaded_at = 0.0
        source_files.source_mirror_etag = None
        source_files.source_mirror_refresh_task = None

    def tearDown(self):
        source_files.SOURCE_MIRROR_MANIFEST_URL = self.old_manifest_url
        source_files.SOURCE_MIRROR_BASE_URL = self.old_base_url
        source_files.source_mirror_manifest = None
        source_files.source_mirror_loaded_at = 0.0
        source_files.source_mirror_etag = None
        source_files.source_mirror_refresh_task = None

    def test_mirrored_source_url_uses_manifest_and_ttl_config(self):
        manifest = {
            "files": {
                "https://example.com/a.pdf": {"path": "archives1/example/a.pdf"},
            }
        }
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = manifest
        with patch.object(source_files.source_client, "get", AsyncMock(return_value=response)), \
                patch.object(source_files, "SOURCE_MIRROR_TTL_SECONDS", 600):
            result = asyncio.run(source_files.mirrored_source_url("https://example.com/a.pdf"))
        self.assertEqual(result, "https://example.com/resolve/main/archives1/example/a.pdf")

    def test_mirrored_source_url_skips_missing_and_unsafe_paths(self):
        manifest = {
            "files": {
                "https://example.com/ok.pdf": {"path": "archives1/ok.pdf"},
                "https://example.com/up.pdf": {"path": "archives2/../secret.pdf"},
                "https://example.com/other.pdf": {"path": "elsewhere/other.pdf"},
            }
        }
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = manifest
        with patch.object(source_files.source_client, "get", AsyncMock(return_value=response)):
            self.assertIsNone(asyncio.run(source_files.mirrored_source_url("https://example.com/missing.pdf")))
            self.assertIsNone(asyncio.run(source_files.mirrored_source_url("https://example.com/up.pdf")))
            self.assertIsNone(asyncio.run(source_files.mirrored_source_url("https://example.com/other.pdf")))

    def test_mirrored_source_url_cached_within_ttl(self):
        manifest = {
            "files": {"https://example.com/a.pdf": {"path": "archives1/a.pdf"}}
        }
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = manifest
        get_mock = AsyncMock(return_value=response)
        with patch.object(source_files.source_client, "get", get_mock), \
                patch.object(source_files, "SOURCE_MIRROR_TTL_SECONDS", 3600):
            first = asyncio.run(source_files.mirrored_source_url("https://example.com/a.pdf"))
            second = asyncio.run(source_files.mirrored_source_url("https://example.com/a.pdf"))
        self.assertEqual(first, second)
        get_mock.assert_awaited_once()

    def test_mirrored_source_url_disabled_without_manifest_url(self):
        source_files.SOURCE_MIRROR_MANIFEST_URL = ""
        with patch.object(source_files.source_client, "get", AsyncMock()) as get_mock:
            result = asyncio.run(source_files.mirrored_source_url("https://example.com/a.pdf"))
        self.assertIsNone(result)
        get_mock.assert_not_awaited()

    def test_refresh_keeps_old_manifest_on_failure(self):
        source_files.source_mirror_manifest = {
            "files": {"https://example.com/a.pdf": {"path": "archives1/a.pdf"}}
        }
        source_files.source_mirror_loaded_at = 0.0
        with patch.object(source_files.source_client, "get", AsyncMock(side_effect=RuntimeError("boom"))):
            asyncio.run(source_files.refresh_source_mirror_manifest())
        self.assertEqual(
            source_files.source_mirror_manifest["files"]["https://example.com/a.pdf"]["path"],
            "archives1/a.pdf",
        )

    def test_refresh_revalidates_manifest_with_etag(self):
        first = MagicMock(status_code=200, headers={"etag": '"manifest-v1"'})
        first.raise_for_status.return_value = None
        first.json.return_value = {"files": {"https://example.com/a.pdf": {"path": "archives1/a.pdf"}}}
        unchanged = MagicMock(status_code=304, headers={})
        get_mock = AsyncMock(side_effect=[first, unchanged])
        with patch.object(source_files.source_client, "get", get_mock):
            asyncio.run(source_files.refresh_source_mirror_manifest())
            asyncio.run(source_files.refresh_source_mirror_manifest())
        self.assertEqual(source_files.source_mirror_etag, '"manifest-v1"')
        self.assertEqual(get_mock.await_args_list[1].kwargs["headers"], {"If-None-Match": '"manifest-v1"'})

    def test_expired_manifest_is_served_while_refresh_runs(self):
        async def scenario():
            source_files.source_mirror_manifest = {"files": {"https://example.com/a.pdf": {"path": "archives1/a.pdf"}}}
            source_files.source_mirror_refresh_task = None
            expired_at = source_files.time.monotonic() - source_files.SOURCE_MIRROR_TTL_SECONDS - 1
            source_files.source_mirror_loaded_at = expired_at
            started = asyncio.Event()
            release = asyncio.Event()

            async def delayed_get(*_args, **_kwargs):
                started.set()
                await release.wait()
                raise RuntimeError("offline")

            with patch.object(source_files.source_client, "get", AsyncMock(side_effect=delayed_get)):
                try:
                    entry = await source_files.source_mirror_entry("https://example.com/a.pdf")
                    self.assertEqual(entry, {"path": "archives1/a.pdf"})
                    task = source_files.source_mirror_refresh_task
                    self.assertIsNotNone(task)
                    await asyncio.wait_for(started.wait(), timeout=5)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    task = source_files.source_mirror_refresh_task
                    if task is not None:
                        await asyncio.wait_for(task, timeout=5)
            self.assertGreater(source_files.source_mirror_loaded_at, expired_at)

        # A newly started host can have a monotonic clock smaller than the TTL.
        # Patch only the module's clock, not the event loop's timeout clock.
        for early_uptime in (False, True):
            with self.subTest(early_uptime=early_uptime), patch.object(source_files, "time", wraps=time) as clock:
                if early_uptime:
                    clock.monotonic.return_value = 1.0
                asyncio.run(scenario())

    def test_warmup_loop_exits_early_when_disabled(self):
        source_files.SOURCE_MIRROR_MANIFEST_URL = ""
        with patch.object(source_files, "refresh_source_mirror_manifest", AsyncMock()) as refresh_mock:
            asyncio.run(source_files.source_mirror_warmup_loop())
        refresh_mock.assert_not_awaited()

    def test_open_mirrored_source_response_uses_direct_fetch(self):
        upstream = FakeStreamResponse(b"mirror bytes", status_code=206)
        upstream.headers.update({"content-range": "bytes 0-6/100", "content-length": "7"})
        send = AsyncMock(return_value=upstream)
        with patch.object(source_files, "mirrored_source_url", AsyncMock(return_value="https://example.com/resolve/main/archives1/a.pdf")), \
                patch.object(source_files.source_client, "send", send), \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)) as open_source:
            response, mirrored = asyncio.run(source_files.open_mirrored_source_response(
                "https://raw.githubusercontent.com/org/repo/main/a.pdf", {"Range": "bytes=0-6"}))
        self.assertTrue(mirrored)
        self.assertEqual(response.status_code, 206)
        request = send.call_args.args[0]
        self.assertTrue(send.call_args.kwargs.get("follow_redirects"))
        self.assertEqual(request.headers["range"], "bytes=0-6")
        open_source.assert_not_awaited()

    def test_open_mirrored_source_response_falls_back_when_mirror_fails(self):
        upstream = FakeStreamResponse(b"upstream bytes")
        with patch.object(source_files, "mirrored_source_url", AsyncMock(return_value="https://example.com/resolve/main/archives1/a.pdf")), \
                patch.object(source_files.source_client, "send", AsyncMock(side_effect=RuntimeError("mirror down"))), \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)) as open_source:
            response, mirrored = asyncio.run(source_files.open_mirrored_source_response(
                "https://raw.githubusercontent.com/org/repo/main/a.pdf", None))
        self.assertFalse(mirrored)
        self.assertEqual(response.status_code, 200)
        open_source.assert_awaited_once_with("https://raw.githubusercontent.com/org/repo/main/a.pdf")

    def test_open_mirrored_source_response_skips_mirror_for_foreign_hosts(self):
        upstream = FakeStreamResponse(b"upstream bytes")
        with patch.object(source_files, "mirrored_source_url", AsyncMock()) as mirrored, \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)) as open_source:
            response, mirror_flag = asyncio.run(source_files.open_mirrored_source_response("https://example.com/a.pdf", None))
        self.assertFalse(mirror_flag)
        self.assertEqual(response.status_code, 200)
        mirrored.assert_not_awaited()
        open_source.assert_awaited_once_with("https://example.com/a.pdf")

    def test_open_mirrored_response_rejects_foreign_host(self):
        with patch.object(source_files.source_client, "send", AsyncMock()) as send:
            with self.assertRaises(ValueError):
                asyncio.run(source_files.open_mirrored_response("https://evil.example.org/x.pdf", None))
        send.assert_not_awaited()


class SourceCacheRevalidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_etag_matches_supports_quoted_weak_and_star(self):
        self.assertTrue(source_files.etag_matches('"abc"', "abc"))
        self.assertTrue(source_files.etag_matches("abc", "abc"))
        self.assertTrue(source_files.etag_matches('W/"abc"', "abc"))
        self.assertTrue(source_files.etag_matches('"other", "abc"', "abc"))
        self.assertTrue(source_files.etag_matches("*", "abc"))
        self.assertFalse(source_files.etag_matches('"def"', "abc"))
        self.assertFalse(source_files.etag_matches("", "abc"))

    def test_source_mirror_entry_returns_manifest_entry(self):
        manifest = {"files": {"https://example.com/a.pdf": {"path": "archives1/a.pdf", "sha256": "abc123"}}}
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = manifest
        with patch.object(source_files, "SOURCE_MIRROR_MANIFEST_URL", "https://example.com/manifest.json"), \
                patch.object(source_files, "SOURCE_MIRROR_BASE_URL", "https://example.com/resolve/main"), \
                patch.object(source_files, "source_mirror_manifest", None), \
                patch.object(source_files, "source_mirror_loaded_at", 0.0), \
                patch.object(source_files.source_client, "get", AsyncMock(return_value=response)):
            entry = asyncio.run(source_files.source_mirror_entry("https://example.com/a.pdf"))
        self.assertEqual(entry["sha256"], "abc123")

    def test_mirrored_inline_returns_304_without_upstream_fetch(self):
        entry = {"path": "archives1/aa/a.pdf", "sha256": "abc123"}
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.pdf"])), \
                patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=entry)), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock()) as open_response:
            response = self.client.get("/api/download/source/doc-1?inline=true", headers={"If-None-Match": '"abc123"'})
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.headers["etag"], '"abc123"')
        self.assertEqual(response.headers["cache-control"], "public, max-age=86400, s-maxage=86400")
        open_response.assert_not_awaited()

    def test_mirrored_inline_sends_sha256_etag_and_long_cache(self):
        entry = {"path": "archives1/aa/a.pdf", "sha256": "abc123"}
        upstream = FakeStreamResponse(b"PDF bytes")
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.pdf"])), \
                patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=entry)), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock(return_value=(upstream, True))):
            response = self.client.get("/api/download/source/doc-1?inline=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["etag"], '"abc123"')
        self.assertEqual(response.headers["cache-control"], "public, max-age=86400, s-maxage=86400")
        self.assertEqual(response.headers["x-bha-source-mirror"], "huggingface")

    def test_mirrored_inline_skips_304_for_range_requests(self):
        entry = {"path": "archives1/aa/a.pdf", "sha256": "abc123"}
        upstream = FakeStreamResponse(b"partial", status_code=206)
        upstream.headers.update({"content-range": "bytes 0-6/100", "content-length": "7"})
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.pdf"])), \
                patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=entry)), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock(return_value=(upstream, True))) as open_response:
            response = self.client.get("/api/download/source/doc-1?inline=true", headers={"Range": "bytes=0-6", "If-None-Match": '"abc123"'})
        self.assertEqual(response.status_code, 206)
        open_response.assert_awaited()

    def test_foreign_host_inline_never_looks_up_manifest(self):
        upstream = FakeStreamResponse(b"PDF bytes")
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://example.com/a.pdf"])), \
                patch.object(source_files, "source_mirror_entry", AsyncMock()) as entry_mock, \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)):
            response = self.client.get("/api/download/source/doc-1?inline=true", headers={"If-None-Match": '"whatever"'})
        self.assertEqual(response.status_code, 200)
        entry_mock.assert_not_awaited()

    def test_mirrored_without_sha256_falls_back_to_upstream_etag(self):
        upstream = FakeStreamResponse(b"PDF bytes")
        upstream.headers["etag"] = '"upstream-hash"'
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.pdf"])), \
                patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=None)), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock(return_value=(upstream, True))):
            response = self.client.get("/api/download/source/doc-1?inline=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["etag"], '"upstream-hash"')

    def test_mirrored_sha256_etag_takes_precedence_over_upstream(self):
        upstream = FakeStreamResponse(b"PDF bytes")
        upstream.headers["etag"] = '"upstream-hash"'
        entry = {"path": "archives1/aa/a.pdf", "sha256": "abc123"}
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.pdf"])), \
                patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=entry)), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock(return_value=(upstream, True))):
            response = self.client.get("/api/download/source/doc-1?inline=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["etag"], '"abc123"')

    def test_download_bypasses_mirror_and_fetches_original(self):
        upstream = FakeStreamResponse(b"original jpg bytes", content_type="image/jpeg")
        entry = {"path": "archives1/aa/a.jpg", "sha256": "abc123"}
        with patch.object(source_files, "get_source_doc", return_value=source_doc(["https://raw.githubusercontent.com/o/r/main/a.jpg"])), \
                patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=entry)), \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock()) as open_mirrored, \
                patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)) as open_source:
            response = self.client.get("/api/download/source/doc-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
        self.assertNotIn("x-bha-source-mirror", response.headers)
        self.assertIn(".jpg", response.headers["content-disposition"])
        open_source.assert_awaited_once()
        open_mirrored.assert_not_awaited()

    def test_source_zip_download_fetches_original_not_mirror(self):
        upstream = FakeStreamResponse(b"x" * 16)
        with patch.object(source_files, "open_source_response", AsyncMock(return_value=upstream)) as open_source, \
                patch.object(source_files, "open_mirrored_source_response", AsyncMock()) as open_mirrored, \
                tempfile.TemporaryDirectory() as directory:
            size = asyncio.run(source_files.download_source_to_path(
                "https://raw.githubusercontent.com/o/r/main/a.jpg", Path(directory) / "out", 100
            ))
        self.assertEqual(size, 16)
        open_source.assert_awaited_once()
        open_mirrored.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
