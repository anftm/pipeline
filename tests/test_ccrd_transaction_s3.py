"""The CCRD journal adapter cannot modify production pointer or other keys."""
import hashlib
import unittest
from io import BytesIO
from unittest.mock import Mock

from botocore.exceptions import ClientError

from scripts import ccrd_lifecycle
from scripts import ccrd_transaction as tx
from scripts import ccrd_transaction_s3 as s3


class CcrdTransactionS3Tests(unittest.TestCase):
    def setUp(self):
        self.backing = Mock(); self.backing._location.return_value = ('vomebook', 'ccrd-index')
        self.client = self.backing._client.return_value; self.store = s3.S3Store(self.backing)
        self.txid = 'a' * 32

    def test_exact_head_read_and_compare_conditions(self):
        raw = ccrd_lifecycle.canonical({'version': 1, 'kind': 'ccrd-cw0-transaction-head',
                                        'transaction': self.txid, 'sequence': 0, 'record': 'b' * 64})
        self.client.get_object.return_value = {'Body': BytesIO(raw), 'ContentLength': len(raw), 'ETag': '"v"'}
        self.assertEqual(self.store.read_head(), (raw, '"v"'))
        self.store.put_head(raw, None); values = self.client.put_object.call_args.kwargs
        self.assertEqual(values['Key'], tx.HEAD); self.assertEqual(values['IfNoneMatch'], '*')
        self.store.put_head(raw, '"v"'); self.assertEqual(self.client.put_object.call_args.kwargs['IfMatch'], '"v"')
        with self.assertRaises(ValueError): self.store.put_head(raw, 'W/"weak"')

    def test_evidence_content_address_and_wrong_key_rejection(self):
        raw = b'{"fixture":true}\n'; digest = hashlib.sha256(raw).hexdigest()
        key = f'{tx.ROOT}{self.txid}/evidence/{digest}.json'
        self.store.put_evidence(key, raw); values = self.client.put_object.call_args.kwargs
        self.assertEqual(values['Key'], key); self.assertEqual(values['IfNoneMatch'], '*')
        self.client.get_object.return_value = {'Body': BytesIO(raw), 'ContentLength': len(raw), 'ETag': '"v"'}
        self.assertEqual(self.store.read_evidence(key), raw)
        with self.assertRaises(ValueError): self.store.put_evidence('current.json', raw)
        with self.assertRaises(ValueError): self.store.put_evidence(key, b'wrong')
        with self.assertRaises(ValueError): self.store.read_record(key)

    def test_missing_truncated_and_wrong_bucket_are_not_valid_state(self):
        error = ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'missing'},
                             'ResponseMetadata': {'HTTPStatusCode': 404}}, 'GetObject')
        self.client.get_object.side_effect = error
        self.assertEqual(self.store.read_head(), (None, None))
        self.client.get_object.side_effect = None
        self.client.get_object.return_value = {'Body': BytesIO(b'ab'), 'ContentLength': 3, 'ETag': '"v"'}
        with self.assertRaisesRegex(ValueError, 'truncated'): self.store.read_head()
        self.backing._location.return_value = ('vomebook', 'other')
        with self.assertRaisesRegex(ValueError, 'bucket mapping'): self.store.read_head()


if __name__ == '__main__':
    unittest.main()
