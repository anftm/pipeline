import gzip
import hashlib
import io
import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError

from scripts import reader_image_history_parent as parent


def s3_error(code, status):
    return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'MockS3')


class BoundedSpool(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= parent.BLOCK
        return super().read(size)

    def write(self, data):
        assert len(data) <= parent.BLOCK
        return super().write(data)


class Storage:
    def __init__(self):
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 1}))
        self.objects, self.gets, self.puts, self.bodies = {}, [], [], []
        self.get_errors, self.get_headers, self.bad_ack = {}, {}, {}
        self.before_put = lambda: None
        self.fault = None

    def get_object(self, *, Bucket, Key, IfMatch=None):
        assert Bucket == parent.BUCKET
        self.gets.append((Key, IfMatch))
        errors = self.get_errors.get(Key)
        if errors:
            raise errors.pop(0)
        if Key not in self.objects:
            raise s3_error('NoSuchKey', 404)
        raw, tag = self.objects[Key]
        if IfMatch is not None and IfMatch != tag:
            raise s3_error('PreconditionFailed', 412)
        body = BoundedSpool(raw)
        self.bodies.append(body)
        return {'Body': body, 'ContentLength': len(raw), 'ETag': tag,
                'ResponseMetadata': {'HTTPStatusCode': 200}} | self.get_headers.get(Key, {})

    def put_object(self, *, Bucket, Key, Body, ContentLength, ContentType, **condition):
        assert Bucket == parent.BUCKET
        assert condition == {'IfNoneMatch': '*'}
        raw = Body.read()
        assert len(raw) == ContentLength
        self.puts.append((Key, ContentType, condition))
        self.before_put()
        if self.fault == 'before':
            raise TimeoutError('request lost')
        if Key in self.objects:
            raise s3_error('ConditionalRequestConflict', 409)
        tag = f'"put-{len(self.puts)}"'
        self.objects[Key] = raw, tag
        if self.fault == 'after':
            raise TimeoutError('response lost')
        return {'ETag': tag, 'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}} | self.bad_ack


class CatalogParentTests(unittest.TestCase):
    def setUp(self):
        self.c = Storage()
        self.original = b'legacy reviewed bytes' * 73000
        self.c.objects[parent.CATALOG] = self.original, '"parent"'
        self.ref = self.archive()
        self.v1 = f'reader-index/gc/image-history-catalog-parents/v1/{self.ref.digest}.json'
        self.v2 = f'reader-index/gc/image-history-catalog-parents/v2/{self.ref.digest}.json.gz'
        self.naps = self.enterContext(patch.object(parent, 'sleep'))
        self.addCleanup(lambda: self.assertTrue(all(body.closed for body in self.c.bodies)))

    def archive(self):
        return parent.CatalogParent(self.c, digest=hashlib.sha256(self.original).hexdigest(),
                                    size=len(self.original), etag='"parent"')

    def stage(self, local=None):
        return self.ref.stage(io.BytesIO() if local is None else local, guard=lambda: True)

    def test_stage_then_recover_after_live_catalog_changes(self):
        local = BoundedSpool(b'junk')
        self.assertEqual(self.ref.path, self.v2)
        self.assertEqual(self.stage(local), '"put-1"')
        self.assertEqual(local.tell(), 0)
        self.assertEqual(local.getvalue(), self.original)
        self.assertEqual(self.c.gets, [(parent.CATALOG, '"parent"'), (self.v1, None),
                                      (self.v2, None), (self.v2, None)])
        packed, _ = self.c.objects[self.v2]
        self.assertLess(len(packed), len(self.original))
        self.assertEqual(gzip.decompress(packed), self.original)
        self.assertEqual(self.stage(local), '"put-1"')
        self.assertEqual(self.c.puts, [(self.v2, 'application/gzip', {'IfNoneMatch': '*'})])
        self.c.objects[parent.CATALOG] = b'changed', '"changed"'
        self.c.gets.clear()
        self.assertEqual(self.archive().load(local), '"put-1"')
        self.assertEqual(local.tell(), 0)
        self.assertEqual(local.getvalue(), self.original)
        self.assertEqual(self.c.gets, [(self.v2, None)])

    def test_stage_and_load_incompressible_binary_across_blocks(self):
        self.original = random.Random(13).randbytes(parent.BLOCK + 50)
        self.c.objects[parent.CATALOG] = self.original, '"parent"'
        self.ref = self.archive()
        self.stage(BoundedSpool())
        self.assertGreater(len(self.c.objects[self.ref.path][0]), len(self.original))
        local = BoundedSpool()
        self.archive().load(local)
        self.assertEqual(local.getvalue(), self.original)

    def test_stage_prefers_verified_v1_and_reports_its_path_and_tag(self):
        self.c.objects[self.v1] = self.original, '"old"'
        self.c.objects[self.v2] = gzip.compress(self.original), '"new"'
        local = io.BytesIO()
        self.assertEqual(self.stage(local), '"old"')
        self.assertEqual(self.ref.path, self.v1)
        self.assertEqual(local.read(), self.original)
        self.assertEqual(self.c.gets, [(parent.CATALOG, '"parent"'), (self.v1, None)])
        self.assertFalse(self.c.puts)
        self.assertEqual(self.c.objects[self.v1], (self.original, '"old"'))
        self.assertEqual(self.ref.load(local), '"new"')
        self.assertEqual(self.ref.path, self.v2)

    def test_load_falls_back_to_verified_v1_only_on_missing_v2(self):
        self.c.objects[self.v1] = self.original, '"old"'
        del self.c.objects[parent.CATALOG]
        local = io.BytesIO(b'junk')
        self.assertEqual(self.ref.load(local), '"old"')
        self.assertEqual(self.ref.path, self.v1)
        self.assertEqual(local.read(), self.original)
        self.assertEqual(self.c.gets, [(self.v2, None), (self.v1, None)])
        self.assertFalse(self.c.puts)

    def test_load_both_missing_does_not_read_live(self):
        with self.assertRaises(parent.S3NotFound):
            self.ref.load(io.BytesIO())
        self.assertEqual(self.c.gets, [(self.v2, None), (self.v1, None)])
        self.assertFalse(self.c.puts)

    def test_load_does_not_fall_back_on_non_key_errors(self):
        self.c.objects[self.v1] = self.original, '"old"'
        for code, status in (('AccessDenied', 403), ('NoSuchBucket', 404), ('Unknown', 404), ('NoSuchKey', 403)):
            with self.subTest(code=code, status=status):
                self.c.gets.clear()
                self.c.get_errors[self.v2] = [s3_error(code, status)]
                with self.assertRaises((ClientError, ValueError)):
                    self.ref.load(io.BytesIO())
                self.assertEqual(self.c.gets, [(self.v2, None)])
        self.assertFalse(self.c.puts)

    def test_load_rejects_corrupt_v2_without_falling_back_or_leaving_partial_output(self):
        self.c.objects[self.v1] = self.original, '"old"'
        packed = gzip.compress(self.original)
        crc_offset = len(packed) - 8
        for bad in (b'not gzip', packed[:-1], packed + b'\0', packed + gzip.compress(b''),
                    packed[:crc_offset] + bytes((packed[crc_offset] ^ 1,)) + packed[crc_offset+1:],
                    gzip.compress(self.original[:-1]), gzip.compress(self.original + b'x' * parent.BLOCK),
                    gzip.compress(b'x' * len(self.original))):
            with self.subTest(length=len(bad)):
                self.c.objects[self.v2] = bad, '"corrupt"'
                self.c.gets.clear()
                local = BoundedSpool(b'junk')
                with self.assertRaises(ValueError):
                    self.ref.load(local)
                self.assertEqual(local.getvalue(), b'')
                self.assertEqual(self.c.gets, [(self.v2, None)])
        self.assertFalse(self.c.puts)

    def test_load_rejects_invalid_v2_response_metadata(self):
        self.c.objects[self.v1] = self.original, '"old"'
        packed = gzip.compress(self.original)
        self.c.objects[self.v2] = packed, '"new"'
        for headers in ({'ContentLength': len(packed) + 1}, {'ContentLength': len(packed) - 1},
                        {'ContentLength': True}, {'ContentLength': len(self.original) * 2},
                        {'ResponseMetadata': {'HTTPStatusCode': 206}}, {'ETag': 'W/"new"'}):
            with self.subTest(headers=headers):
                self.c.get_headers[self.v2] = headers
                self.c.gets.clear()
                with self.assertRaises(ValueError):
                    self.ref.load(io.BytesIO())
                self.assertEqual(self.c.gets, [(self.v2, None)])

    def test_lost_archive_acknowledgement_requires_observation(self):
        self.c.fault = 'after'
        with self.assertRaises(TimeoutError):
            self.stage()
        self.assertEqual(self.c.gets, [(parent.CATALOG, '"parent"'), (self.v1, None), (self.v2, None)])
        local = io.BytesIO()
        self.assertEqual(self.archive().load(local), '"put-1"')
        self.assertEqual(local.read(), self.original)
        self.assertEqual(len(self.c.puts), 1)

    def test_conditional_conflict_polls_only_v2_and_accepts_exact_winner(self):
        def race():
            self.c.objects[self.v2] = gzip.compress(self.original), '"winner"'
            self.c.get_errors[self.v2] = [s3_error('NotFound', 404)]
        self.c.before_put = race
        self.assertEqual(self.stage(), '"winner"')
        self.assertEqual(self.c.gets, [(parent.CATALOG, '"parent"'), (self.v1, None),
                                      (self.v2, None), (self.v2, None), (self.v2, None)])
        self.naps.assert_called_once()
        self.assertEqual(len(self.c.puts), 1)

    def test_success_ack_still_requires_exact_readback_with_same_etag(self):
        self.c.bad_ack = {'ETag': '"different"'}
        with self.assertRaisesRegex(ValueError, 'independent readback'):
            self.stage()
        self.assertEqual(self.c.gets.count((self.v2, None)), 2)
        self.assertEqual(len(self.c.puts), 1)

    def test_missing_readback_polls_a_bounded_number_of_times_and_never_replays(self):
        self.c.before_put = lambda: self.c.get_errors.update({self.v2: [s3_error('NoSuchKey', 404)] * 4})
        with self.assertRaises(parent.S3NotFound):
            self.stage()
        self.assertEqual(self.c.gets.count((self.v2, None)), 4)
        self.assertEqual(self.naps.call_count, 2)
        self.assertEqual(len(self.c.puts), 1)

    def test_condition_conflict_with_corrupt_winner_is_not_polled_or_replaced(self):
        self.c.before_put = lambda: self.c.objects.update({self.v2: (b'wrong', '"winner"')})
        with self.assertRaisesRegex(ValueError, 'gzip'):
            self.stage()
        self.naps.assert_not_called()
        self.assertEqual(len(self.c.puts), 1)
        self.assertEqual(self.c.objects[self.v2], (b'wrong', '"winner"'))

    def test_guard_and_parent_or_archive_mismatch_cause_no_put(self):
        with self.assertRaisesRegex(ValueError, 'active migration fence'):
            self.ref.stage(io.BytesIO(), guard=lambda: False)
        permits = iter([True, False])
        with self.assertRaisesRegex(ValueError, 'fence changed'):
            self.ref.stage(io.BytesIO(), guard=lambda: next(permits))
        self.c.objects[self.v1] = b'x' * len(self.original), '"foreign"'
        self.c.objects[self.v2] = gzip.compress(self.original), '"new"'
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            self.stage()
        del self.c.objects[self.v1]
        self.c.objects[self.v2] = gzip.compress(b'x' * len(self.original)), '"foreign"'
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            self.stage()
        self.c.objects[parent.CATALOG] = self.original, '"drift"'
        with self.assertRaises(ClientError):
            self.stage()
        self.assertFalse(self.c.puts)
