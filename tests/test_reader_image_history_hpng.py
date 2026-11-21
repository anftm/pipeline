"""Exercise the h.PNG bridge against local S3 and HTTP transports only."""
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

import httpx
from botocore.exceptions import ClientError

from scripts import reader_image_history_hpng as subject
from tests import test_reader_image_history_runtime as fixture

v3, image, legacy = subject.v3, subject.image, subject.legacy


class S3:
    def __init__(self, objects):
        self.objects, self.puts, self.types = objects, [], {}
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 1}))
        self.lost = None

    @staticmethod
    def error(code, status):
        return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'MockS3')

    def head_object(self, *, Bucket, Key):
        assert Bucket == 'reader-assets-v2'
        if Key not in self.objects:
            raise self.error('NoSuchKey', 404)
        raw, tag = self.objects[Key]
        return {'ETag': tag, 'ContentLength': len(raw), 'ResponseMetadata': {'HTTPStatusCode': 200}}

    def get_object(self, *, Bucket, Key, IfMatch=None, Range=None):
        result = self.head_object(Bucket=Bucket, Key=Key)
        if IfMatch is not None and result['ETag'] != IfMatch:
            raise self.error('PreconditionFailed', 412)
        raw = self.objects[Key][0]
        if Range is not None:
            first, last = map(int, Range.removeprefix('bytes=').split('-'))
            result.update(ContentRange=f'bytes {first}-{last}/{len(raw)}', ContentLength=last-first+1,
                          ResponseMetadata={'HTTPStatusCode': 206})
            raw = raw[first:last+1]
        return {**result, 'Body': io.BytesIO(raw)}

    def put_object(self, *, Bucket, Key, Body, ContentType, ContentLength=None, **condition):
        assert Bucket == 'reader-assets-v2'
        assert condition == ({'IfMatch': self.objects[Key][1]} if Key in self.objects else {'IfNoneMatch': '*'})
        self.puts.append((Key, condition))
        raw = Body if isinstance(Body, bytes) else Body.read()
        assert ContentLength is None or ContentLength == len(raw)
        tag = f'"put-{len(self.puts)}"'
        self.objects[Key] = (raw, tag)
        self.types[Key] = ContentType
        if self.lost == Key:
            self.lost = None
            raise TimeoutError('committed, response lost')
        return {'ETag': tag, 'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        f = fixture.RuntimeTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        r = f.reviewed
        fields = r.report | {'source_dimensions': [1, 1]}
        self.s3 = S3(dict(f.bucket.objects) | {path: (raw, f.tags.get(path, '"fixture"')) for path, raw in f.reader.items()})
        self.pins = r.transition.canonical.preflight
        paths = {*f.reader, image.CANONICAL, f.shard_path} - {legacy.CATALOG}
        fields['reader_inputs'] = [{'bucket': v3.ASSETS, 'path': path, 'sha256': image._sha(self.s3.objects[path][0])}
                                   for path in paths]
        r = replace(r, report_raw=v3.encode(fields))
        self.mime = 'image/avif'
        self.served = {}
        self.http = httpx.Client(transport=httpx.MockTransport(self.resolve))
        self.addCleanup(self.http.close)
        self.enterContext(patch.multiple(subject, REVIEWED_SHA256=r.sha256, OPERATION=r.transition.canonical.operation))
        hub = SimpleNamespace(repo_info=lambda *a, **kw: SimpleNamespace(sha=self.pins.current_source.revision))
        self.op = subject.Operator(r, s3=self.s3, http=self.http, hub=hub)
        # Unrelated large Search/build inputs use small deterministic fixtures;
        # publication/journal, catalog and transport implementations are real.
        self.enterContext(patch.object(self.op, '_search', return_value=self.pins.search_sha256))
        self.enterContext(patch.object(subject.preview, '_decoded_image', return_value=((1, 1), None)))
        self.enterContext(patch.object(subject.preview, '_build_sidecar', side_effect=lambda inputs:
            subject.catalog._sidecar(r.before_sidecar if inputs.override == r.transition.canonical.parent else r.after_sidecar)))
        self.fence = True
        self.labels = []

    def resolve(self, request):
        path = unquote(request.url.path)
        self.assertEqual(request.method, 'GET')
        if path.startswith('/datasets/'):
            for pin in (self.pins.source, self.pins.current_source):
                if path.endswith(f'/resolve/{pin.revision}/{pin.path}'):
                    return httpx.Response(200, content=b'png')
            self.fail(f'wrong pinned revision: {path}')
        self.assertTrue(path.startswith('/buckets/vomebook/reader-assets-v2/resolve/'))
        path = path.split('/resolve/', 1)[1]
        if path not in self.s3.objects:
            return httpx.Response(404)
        mime = 'image/webp' if path.endswith('.webp') else self.mime if path.endswith('.avif') else 'application/json'
        raw = self.served.get(path, self.s3.objects[path][0])
        if 'Range' in request.headers:
            tag = self.s3.objects[path][1]
            self.assertEqual(request.headers['If-Match'], tag)
            lo, hi = map(int, request.headers['Range'].removeprefix('bytes=').split('-'))
            return httpx.Response(206, content=raw[lo:hi+1], headers={
                'ETag': tag, 'Content-Range': f'bytes {lo}-{hi}/{len(raw)}', 'Content-Type': mime})
        return httpx.Response(200, content=raw, headers={'Content-Type': mime})

    def guard(self, phase):
        self.labels.append(phase)
        return self.fence and phase != getattr(self, 'deny_phase', None)

    def test_verify_is_read_only_and_checks_independent_catalog(self):
        self.assertFalse(self.op.verify()['write_admission'])
        self.served[legacy.CATALOG] = b'!' + self.s3.objects[legacy.CATALOG][0][1:]
        with self.assertRaisesRegex(ValueError, 'serving view'):
            self.op.verify()
        self.assertEqual(self.s3.puts, [])

    def test_legacy_and_v3_lost_commit_are_observed_without_replay(self):
        self.op.stage_parent(guard=self.guard)
        self.s3.lost = legacy.CATALOG
        with self.assertRaisesRegex(TimeoutError, 'response lost'):
            self.op.publish_legacy(guard=self.guard)
        with self.assertRaisesRegex(ValueError, 'explicit resume flags'):
            self.op.publish_legacy(guard=self.guard)
        result = self.op.publish_legacy(guard=self.guard, resume_indexes=True, resume_legacy=True)
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(self.s3.types[self.pins.new.path], 'image/avif')
        self.s3.lost = v3.POINTER
        with self.assertRaisesRegex(TimeoutError, 'response lost'):
            self.op.promote(guard=self.guard)
        writes = list(self.s3.puts)
        self.assertTrue(self.op.promote(guard=self.guard, observe_only=True)['confirmed'])
        self.assertEqual(self.s3.puts, writes)
        self.assertEqual([key for key, _ in writes].count(legacy.CATALOG), 1)
        self.assertEqual([key for key, _ in writes].count(v3.POINTER), 1)

    def test_fence_is_checked_at_transport_and_bad_serving_mime_blocks_mutable_puts(self):
        self.deny_phase = 'put:' + self.op.archive.path
        with self.assertRaisesRegex(ValueError, 'fence'):
            self.op.stage_parent(guard=self.guard)
        self.assertEqual(self.s3.puts, [])
        self.deny_phase = None
        self.op.stage_parent(guard=self.guard)
        self.mime = 'application/octet-stream'
        with self.assertRaisesRegex(ValueError, 'served MIME'):
            self.op.publish_legacy(guard=self.guard)
        self.assertEqual([key for key, _ in self.s3.puts],
                         [self.op.archive.path, self.op.reviewed.processing_path, self.pins.new.path])


if __name__ == '__main__':
    unittest.main()
