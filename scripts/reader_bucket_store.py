#!/usr/bin/env python3
"""S3 adapter for current Reader buckets."""

from __future__ import annotations

import concurrent.futures
import gzip
import json
import os
import posixpath
import random
import re
import tempfile
import time
import threading
from pathlib import Path
from urllib.parse import quote

try:
    from .shared import batch_bucket_files_with_retry
    from .reader_gc_cache import cacheable, configured_cache, RequestPacer
except ImportError:
    from shared import batch_bucket_files_with_retry
    from reader_gc_cache import cacheable, configured_cache, RequestPacer


class S3NotFound(FileNotFoundError):
    pass


def resolver_retry_delay(error: BaseException, attempt: int) -> int:
    """Use HF's explicit reset countdown, including the response-body form."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return min(300, max(1, int(float(retry_after))))
        except (OverflowError, TypeError, ValueError):
            pass
    details = f"{error} {getattr(response, 'text', '')}"
    match = re.search(r"\bretry\s+after\s+(\d+(?:\.\d+)?)\s+seconds\b", details, re.IGNORECASE)
    if match:
        return min(300, max(1, int(float(match.group(1)))))
    return min(120, 2 ** min(attempt, 7))


def retry_read(operation):
    import httpx
    from huggingface_hub.errors import HfHubHTTPError
    for attempt in range(12):
        try:
            return operation()
        except (HfHubHTTPError, httpx.HTTPStatusError) as error:
            status = getattr(error.response, "status_code", None)
            if status not in {408, 429, 500, 502, 503, 504} or attempt == 11:
                raise
            delay = resolver_retry_delay(error, attempt)
        except httpx.TransportError:
            if attempt == 11:
                raise
            delay = min(120, 2 ** attempt)
        print(f"transient Reader bucket read error; retrying in {delay}s "
              f"(attempt {attempt + 1}/12)", flush=True)
        time.sleep(delay + random.uniform(0, 1.5))


class HubBucketStore:
    """Current bucket API access with independent credentials for OCR inputs."""

    def __init__(self):
        from huggingface_hub import HfApi
        self.api = HfApi(token=os.environ.get("HF_TOKEN"))
        self.object_sizes = {}
        self.inventory_progress = None
        self.resolver_lock = threading.Lock()
        self.resolver_next = 0.0

    def pace_resolver(self):
        # The resolver quota is shared by concurrent graph readers in one run.
        with self.resolver_lock:
            now = time.monotonic()
            wait = max(0, self.resolver_next - now)
            self.resolver_next = max(now, self.resolver_next) + 0.3
        if wait:
            time.sleep(wait)

    def token(self, bucket):
        if bucket.startswith("melsm/"):
            return os.environ.get("HF_INPUT_TOKEN")
        return os.environ.get("HF_TOKEN")

    def list_files(self, bucket: str, prefixes: tuple[str, ...]) -> set[str]:
        files = set()
        for prefix in prefixes:
            def listing(prefix=prefix):
                return list(self.api.list_bucket_tree(
                    bucket, prefix=prefix or None, recursive=True, token=self.token(bucket)))
            for item in retry_read(listing):
                if item.type == "file":
                    files.add(item.path)
                    size = getattr(item, "size", None)
                    if type(size) is int and size >= 0:
                        self.object_sizes[(bucket, item.path)] = size
                    if self.inventory_progress and len(files) % 50000 == 0:
                        self.inventory_progress(bucket, len(files))
        return files

    def read_bytes(self, bucket: str, path: str) -> bytes:
        from huggingface_hub.utils import get_session, hf_raise_for_status
        token = self.token(bucket)
        def read():
            self.pace_resolver()
            response = get_session().get(
                f"https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe='/')}",
                headers={"Authorization": f"Bearer {token}"} if token else {},
                follow_redirects=True, timeout=120)
            if response.status_code == 404:
                raise FileNotFoundError(path)
            hf_raise_for_status(response)
            return response.content
        return retry_read(read)

    def put_json(self, bucket: str, path: str, payload: dict) -> None:
        with tempfile.TemporaryDirectory(prefix="reader-gc-state-") as directory:
            target = Path(directory) / "state.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            batch_bucket_files_with_retry(bucket, [(str(target), path)], self.token(bucket))

    def read_graph_json(self, bucket: str, path: str, parser=None):
        from huggingface_hub.utils import get_session, hf_raise_for_status
        try:
            from .reader_gc_stream import graph_json
        except ImportError:
            from reader_gc_stream import graph_json
        parser = parser or graph_json
        token = self.token(bucket)
        def read():
            self.pace_resolver()
            with get_session().stream("GET", f"https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe='/')}",
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    follow_redirects=True, timeout=120) as response:
                if response.status_code == 404:
                    raise FileNotFoundError(path)
                hf_raise_for_status(response)
                return parser(response.iter_bytes(), path.endswith(".gz"))
        return retry_read(read)

    def digest(self, bucket: str, path: str) -> str:
        import hashlib
        from huggingface_hub.utils import get_session, hf_raise_for_status
        token = self.token(bucket)
        def read_digest():
            digest = hashlib.sha256()
            self.pace_resolver()
            with get_session().stream("GET", f"https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe='/')}",
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    follow_redirects=True, timeout=120) as response:
                hf_raise_for_status(response)
                for chunk in response.iter_bytes():
                    digest.update(chunk)
            return digest.hexdigest()
        return retry_read(read_digest)

    def put_bytes(self, bucket: str, path: str, payload: bytes) -> None:
        batch_bucket_files_with_retry(bucket, [(payload, path)], self.token(bucket))

    def stat(self, bucket: str, path: str) -> int:
        key = (bucket, path)
        if key in self.object_sizes:
            return self.object_sizes[key]
        from huggingface_hub.utils import get_session, hf_raise_for_status
        token = self.token(bucket)
        def head():
            response = get_session().head(
                f"https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe='/')}",
                headers={"Authorization": f"Bearer {token}"} if token else {},
                follow_redirects=True, timeout=120)
            if response.status_code == 404:
                raise FileNotFoundError(path)
            hf_raise_for_status(response)
            size = response.headers.get("Content-Length")
            if size is None or not size.isdigit():
                raise RuntimeError(f"bucket object has no exact size: {bucket}/{path}")
            return int(size)
        return retry_read(head)

    def delete(self, bucket: str, paths: list[str]) -> None:
        if bucket not in {"vomebook/reader-assets-v2", "vomebook/pdf-pages-v2", "melsm/pdf-archive-v2",
                          "vomebook/ccrd-index"}:
            raise ValueError("GC deletion must target a current Reader bucket")
        if any(not path or path.startswith("/") or "\\" in path or "\0" in path
               or any(part in {"", ".", ".."} for part in path.split("/")) for path in paths):
            raise ValueError("invalid GC deletion path")
        # All format collectors use the same idempotent, bounded retry path.
        for start in range(0, len(paths), 500):
            batch_bucket_files_with_retry(bucket, [], self.token(bucket), deletions=paths[start:start + 500])


S3_RETRY_CODES = {"408", "429", "500", "502", "503", "504", "InternalError", "ServiceUnavailable", "SlowDown",
                  "RequestTimeout", "RequestTimeTooSkewed", "Throttling", "ThrottlingException"}


def s3_error_code(error: BaseException):
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return None, None
    return (response.get("Error", {}).get("Code"),
            response.get("ResponseMetadata", {}).get("HTTPStatusCode"))


def retry_s3(operation, attempts: int = 8, sleep=None):
    """Retry transient S3 transport/streaming failures; missing objects stay missing."""
    from botocore.exceptions import BotoCoreError, ClientError
    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            return operation()
        except ClientError as error:
            code, status = s3_error_code(error)
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                raise S3NotFound(str(error)) from error
            if (str(code) not in S3_RETRY_CODES and status not in {408, 429, 500, 502, 503, 504}) \
                    or attempt == attempts - 1:
                raise
        except (BotoCoreError, OSError) as error:
            if isinstance(error, FileNotFoundError) or attempt == attempts - 1:
                raise
        delay = min(30, 2 ** attempt) + random.uniform(0, 1)
        print(f"transient Reader S3 request error; retrying in {delay:.1f}s "
              f"(attempt {attempt + 1}/{attempts})", flush=True)
        sleep(delay)


class S3BucketStore:
    def __init__(self) -> None:
        self._access_key = os.environ.get("HF_S3_ACCESS_KEY_ID")
        self._secret_key = os.environ.get("HF_S3_SECRET_ACCESS_KEY")
        if not self._access_key or not self._secret_key:
            raise RuntimeError("HF_S3_ACCESS_KEY_ID and HF_S3_SECRET_ACCESS_KEY are required")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:
            raise RuntimeError("boto3 is required for Reader bucket operations") from error
        self._boto3 = boto3
        # Short socket timeouts turn stalled connections (observed at ~190 s)
        # into quick retries; read_timeout bounds each socket read, not a body.
        self._config = Config(region_name="us-east-1", s3={"addressing_style": "path"},
                              retries={"mode": "adaptive", "max_attempts": 8},
                              connect_timeout=10, read_timeout=20,
                              max_pool_connections=int(os.environ.get("READER_S3_POOL", "128")))
        self._namespace = os.environ.get("HF_S3_NAMESPACE", "vomebook")
        self._input_namespace = os.environ.get("HF_S3_INPUT_NAMESPACE", "melsm")
        self._input_bucket = os.environ.get("HF_S3_INPUT_BUCKET", "pdf-archive-v2")
        self._clients = {}
        self._client_lock = threading.Lock()
        self._pacers = {}
        self.read_cache = configured_cache()
        self.object_etags = {}
        self.object_modified = {}
        self.object_sizes = {}
        # Listing and read-time ETags let the GC graph detect changed roots
        # without re-downloading every root.
        self.root_etags = {}
        self.read_etags = {}
        self.root_predicate = None
        self.list_workers = int(os.environ.get("READER_S3_LIST_WORKERS", "32"))

    def _location(self, bucket: str) -> tuple[str, str]:
        if "/" in bucket:
            return bucket.rsplit("/", 1)
        if bucket == self._input_bucket:
            return self._input_namespace, bucket
        return self._namespace, bucket

    def _client(self, namespace: str):
        separate = namespace == self._input_namespace and namespace != self._namespace
        key = os.environ.get("HF_S3_INPUT_ACCESS_KEY_ID") if separate else self._access_key
        secret = os.environ.get("HF_S3_INPUT_SECRET_ACCESS_KEY") if separate else self._secret_key
        if not key or not secret:
            raise RuntimeError(f"S3 credentials are required for namespace {namespace}")
        cache_key = (namespace, key)
        with self._client_lock:
            if cache_key not in self._clients:
                client = self._boto3.client(
                    "s3", endpoint_url=f"https://s3.hf.co/{namespace}",
                    aws_access_key_id=key, aws_secret_access_key=secret, config=self._config)
                if os.environ.get('READER_GC_S3_RATE'):
                    pacer = RequestPacer(float(os.environ['READER_GC_S3_RATE']))
                    client.meta.events.register('before-send.s3', pacer.before_send)
                    client.meta.events.register('needs-retry.s3', pacer.after_response)
                    self._pacers[namespace] = pacer
                self._clients[cache_key] = client
        return self._clients[cache_key]

    def _remember_etag(self, bucket: str, path: str, response) -> None:
        etag = response.get("ETag") if isinstance(response, dict) else None
        if etag:
            self.__dict__.setdefault("read_etags", {})[(bucket, path)] = etag

    def _shards(self, client, name: str, prefix: str, depth: int = 0):
        """Split a prefix by delimiter so large buckets list in parallel."""
        if depth >= 2:
            return {}, [prefix]
        files, children = {}, []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=name, Prefix=prefix, Delimiter="/"):
            files.update((item["Key"], item) for item in page.get("Contents", []))
            children.extend(item["Prefix"] for item in page.get("CommonPrefixes", []))
            if len(children) > 256:
                return files, [prefix]
        if not children:
            return files, []
        if len(children) >= 16:
            return files, children
        shards = []
        for child in sorted(children):
            direct, nested = self._shards(client, name, child, depth + 1)
            files.update(direct)
            shards.extend(nested)
        return files, shards

    def list_files(self, bucket: str, prefixes: tuple[str, ...]) -> set[str]:
        namespace, name = self._location(bucket)
        client = self._client(namespace)
        def one(prefix):
            result = {}
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=name, Prefix=prefix):
                result.update((item["Key"], item) for item in page.get("Contents", []))
            return result
        items, shards = {}, []
        for prefix in prefixes:
            # Control trees contain thousands of one-file directories. Listing
            # each directory separately multiplies API calls and can leave a
            # collector throttled in its pre-delete barrier for hours. A flat
            # paginated listing still returns every key, size and current ETag.
            if prefix.startswith(('reader-index/', 'control/')):
                shards.append(prefix)
                continue
            direct, found = retry_s3(lambda prefix=prefix: self._shards(client, name, prefix))
            items.update(direct)
            shards.extend(found)
        workers = getattr(self, "list_workers", 32)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(shards) or 1))) as pool:
            for result in pool.map(lambda shard: retry_s3(lambda: one(shard)), shards):
                items.update(result)
        predicate = getattr(self, "root_predicate", None)
        etags = (self.__dict__.setdefault("root_etags", {}).setdefault(bucket, {}) if predicate else None)
        object_etags = self.__dict__.setdefault('object_etags', {}).setdefault(bucket, {})
        for key in list(object_etags):
            if key.startswith(prefixes):
                object_etags.pop(key)
        sizes = self.__dict__.setdefault("object_sizes", {})
        if etags is not None:
            for key in list(etags):
                if key.startswith(prefixes):
                    etags.pop(key)
        for key, item in items.items():
            size = item.get("Size")
            if type(size) is int and size >= 0:
                sizes[(bucket, key)] = size
            if etags is not None and predicate(key):
                etags[key] = item.get("ETag")
            if cacheable(key):
                object_etags[key] = item.get('ETag')
                self.__dict__.setdefault('object_modified', {})[(bucket, key)] = item.get('LastModified')
        return set(items)

    def list_roots(self, bucket: str, parents: set[str]) -> dict[str, str | None]:
        """List only GC root locations: all of reader-index/ plus known index directories."""
        namespace, name = self._location(bucket)
        client = self._client(namespace)
        predicate = getattr(self, "root_predicate", None) or (lambda path: True)
        def flat(prefix):
            items = {}
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=name, Prefix=prefix, Delimiter="/"):
                items.update((item["Key"], item) for item in page.get("Contents", []))
            return items
        def deep(prefix):
            items = {}
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=name, Prefix=prefix):
                items.update((item["Key"], item) for item in page.get("Contents", []))
            return items
        top, children = {}, []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=name, Prefix="reader-index/", Delimiter="/"):
            top.update((item["Key"], item) for item in page.get("Contents", []))
            children.extend(item["Prefix"] for item in page.get("CommonPrefixes", []))
        jobs = [(deep, child) for child in children] + [(flat, parent) for parent in sorted(parents)]
        workers = getattr(self, "list_workers", 32)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs) or 1))) as pool:
            for result in pool.map(lambda job: retry_s3(lambda: job[0](job[1])), jobs):
                top.update(result)
        roots = {key: item.get("ETag") for key, item in top.items() if predicate(key)}
        self.__dict__.setdefault("root_etags", {}).setdefault(bucket, {}).update(roots)
        self.__dict__.setdefault('object_etags', {}).setdefault(bucket, {}).update(
            (key, item.get('ETag')) for key, item in top.items() if cacheable(key))
        return roots

    def _cached(self, bucket, path, parser):
        cache = getattr(self, 'read_cache', None)
        etag = getattr(self, 'object_etags', {}).get(bucket, {}).get(path)
        if cache and cacheable(path):
            hit = cache.get(bucket, path, parser, etag)
            if hit:
                self._remember_etag(bucket, path, {'ETag': etag})
                return hit
        return None

    def _cache_read(self, bucket, path, parser, raw, digest):
        cache = getattr(self, 'read_cache', None)
        if cache and cacheable(path):
            etag = getattr(self, 'read_etags', {}).get((bucket, path))
            cache.put(bucket, path, parser, etag, raw, digest)

    def read_cached_bytes(self, bucket: str, path: str) -> bytes:
        """Caller must list current identities first; ordinary reads stay fresh."""
        cached = self._cached(bucket, path, 'raw-v1')
        if cached:
            return cached[0]
        return self.read_bytes(bucket, path)

    def read_bytes(self, bucket: str, path: str) -> bytes:
        namespace, name = self._location(bucket)
        def read():
            response = self._client(namespace).get_object(Bucket=name, Key=path)
            body = response["Body"]
            try:
                raw = body.read()
            finally:
                body.close()
            self._remember_etag(bucket, path, response)
            import hashlib
            self._cache_read(bucket, path, 'raw-v1', raw, hashlib.sha256(raw).hexdigest())
            return raw
        try:
            return retry_s3(read)
        except Exception as error:
            code, _ = s3_error_code(error)
            if code == "NoSuchBucket":
                raise S3NotFound(path) from error
            raise

    def read_graph_json(self, bucket: str, path: str, parser=None):
        try:
            from .reader_gc_stream import graph_json
        except ImportError:
            from reader_gc_stream import graph_json
        default_parser = parser is None
        if default_parser:
            cached = self._cached(bucket, path, 'graph-json-v1')
            if cached:
                return json.loads(cached[0]), cached[1]
            raw = self._cached(bucket, path, 'raw-v1')
            if raw:
                return graph_json([raw[0]], path.endswith('.gz'))
        parser = parser or graph_json
        namespace, name = self._location(bucket)
        def read():
            response = self._client(namespace).get_object(Bucket=name, Key=path)
            body = response["Body"]
            try:
                result = parser(iter(lambda: body.read(1024 * 1024), b""), path.endswith(".gz"))
            finally:
                body.close()
            self._remember_etag(bucket, path, response)
            if default_parser:
                self._cache_read(bucket, path, 'graph-json-v1',
                                 json.dumps(result[0], ensure_ascii=False, separators=(',', ':')).encode(), result[1])
            return result
        return retry_s3(read)

    def digest(self, bucket: str, path: str) -> str:
        import hashlib
        namespace, name = self._location(bucket)
        def read():
            response = self._client(namespace).get_object(Bucket=name, Key=path)
            body, value = response["Body"], hashlib.sha256()
            try:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    value.update(chunk)
            finally:
                body.close()
            return value.hexdigest()
        return retry_s3(read)

    def put_json(self, bucket: str, path: str, payload: dict) -> None:
        namespace, name = self._location(bucket)
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        retry_s3(lambda: self._client(namespace).put_object(
            Bucket=name, Key=path,
            Body=raw, ContentType="application/json"))

    def existing_files(self, bucket: str, paths: set[str]) -> set[str]:
        namespace, name = self._location(bucket)
        client = self._client(namespace)
        def exists(path):
            try:
                retry_s3(lambda: client.head_object(Bucket=name, Key=path))
                return path
            except FileNotFoundError:
                return None
            except Exception as error:
                response = getattr(error, "response", {})
                code = response.get("Error", {}).get("Code")
                status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                    return None
                raise
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(64, max(1, len(paths)))) as pool:
            return {path for path in pool.map(exists, sorted(paths)) if path}

    def manifest_references(self, bucket: str, paths: set[str]) -> dict[str, set[str]]:
        manifests = {path for path in paths if path.endswith(("/page-manifest.json", "/chapter-manifest.json"))}
        def load(path):
            raw = self.read_bytes(bucket, path)
            payload = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
            root = posixpath.dirname(path)
            if path.endswith("/page-manifest.json"):
                count = payload.get("page_count")
                if type(count) is not int or count < 1:
                    raise ValueError(f"invalid page manifest: {path}")
                return path, {f"{root}/pages/page-{number:06d}.webp" for number in range(1, count + 1)}
            chapters = payload.get("chapters")
            if not isinstance(chapters, list):
                raise ValueError(f"invalid chapter manifest: {path}")
            refs = {posixpath.normpath(posixpath.join(root, item["path"]))
                    for item in chapters if isinstance(item, dict) and isinstance(item.get("path"), str)}
            search = payload.get("search_index")
            if isinstance(search, dict) and isinstance(search.get("path"), str):
                refs.add(posixpath.normpath(posixpath.join(root, search["path"])))
            return path, refs
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, max(1, len(manifests)))) as pool:
            return dict(pool.map(load, sorted(manifests)))

    def delete(self, bucket: str, paths: list[str]) -> None:
        namespace, name = self._location(bucket)
        client = self._client(namespace)
        for start in range(0, len(paths), 1000):
            batch = paths[start:start + 1000]
            def remove():
                response = client.delete_objects(Bucket=name, Delete={
                    "Objects": [{"Key": path} for path in batch], "Quiet": True})
                errors = response.get('Errors', [])
                if errors and all(error.get('Code') in S3_RETRY_CODES for error in errors):
                    from botocore.exceptions import ClientError
                    raise ClientError({'Error': errors[0]}, 'DeleteObjects')
                if errors:
                    raise RuntimeError(f"S3 delete failed for {len(errors)} object(s)")
            retry_s3(remove)
