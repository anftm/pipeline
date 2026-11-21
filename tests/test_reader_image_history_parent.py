import hashlib
import io
import unittest

from botocore.exceptions import ClientError

from scripts import reader_image_history_parent as parent
from tests.test_reader_image_history_store import MemoryS3, S3Error


class Storage(MemoryS3):
    def get_object(self, *, Bucket, Key, IfMatch=None):
        try:
            outcome = super().get_object(Bucket=Bucket, Key=Key)
        except S3Error as error:
            raise ClientError(error.response, 'GetObject') from error
        if IfMatch is not None and IfMatch != outcome['ETag']:
            raise ClientError(S3Error('PreconditionFailed', 412).response, 'GetObject')
        return outcome | {'ResponseMetadata': {'HTTPStatusCode': 200}}

    def put_object(self, *, Body, ContentLength, **args):
        raw = Body.read()
        assert len(raw) == ContentLength
        return super().put_object(Body=raw, **args)


class CatalogParentTests(unittest.TestCase):
    def setUp(self):
        self.c = Storage()
        self.original = b'legacy reviewed bytes' * 73000
        self.c.objects[parent.CATALOG] = self.original, '"parent"'
        self.ref = parent.CatalogParent(self.c, digest=hashlib.sha256(self.original).hexdigest(),
                                        size=len(self.original), etag='"parent"')

    def test_stage_then_recover_after_live_catalog_changes(self):
        local = io.BytesIO(b'junk')
        self.assertEqual(self.ref.stage(local, guard=lambda: True), '"put-1"')
        self.assertEqual(local.read(), self.original)
        self.assertEqual(self.ref.stage(local, guard=lambda: True), '"put-1"')
        self.assertEqual(self.c.puts, [(self.ref.path, {'IfNoneMatch': '*'})])
        self.c.objects[parent.CATALOG] = b'changed', '"changed"'
        self.ref.load(local)
        self.assertEqual(local.read(), self.original)

    def test_lost_archive_acknowledgement_requires_observation(self):
        self.c.fault = 'after'
        with self.assertRaises(TimeoutError):
            self.ref.stage(io.BytesIO(), guard=lambda: True)
        local = io.BytesIO()
        self.assertEqual(self.ref.load(local), '"put-1"')
        self.assertEqual(local.read(), self.original)
        self.assertEqual(len(self.c.puts), 1)

    def test_guard_and_parent_or_archive_mismatch_cause_no_put(self):
        with self.assertRaisesRegex(ValueError, 'active migration fence'):
            self.ref.stage(io.BytesIO(), guard=lambda: False)
        permits = iter([True, False])
        with self.assertRaisesRegex(ValueError, 'fence changed'):
            self.ref.stage(io.BytesIO(), guard=lambda: next(permits))
        self.c.objects[self.ref.path] = b'x' * len(self.original), '"foreign"'
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            self.ref.stage(io.BytesIO(), guard=lambda: True)
        self.c.objects[parent.CATALOG] = self.original, '"drift"'
        with self.assertRaises(ClientError):
            self.ref.stage(io.BytesIO(), guard=lambda: True)
        self.assertFalse(self.c.puts)
