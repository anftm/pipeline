import hashlib
import unittest
from unittest.mock import patch

from scripts import reader_producer_audit_publish as publication


def sample():
    objects = {}
    for category, count in (('receipts', 9), ('writer-inventory', 1), ('candidates', 1), ('intents', 1)):
        for index in range(count):
            raw = (category + ':' + str(index)).encode()
            path = publication.intent.PREFIX + category + '/' + hashlib.sha256(raw).hexdigest() + '.json'
            objects[path] = raw
    return objects


class AuditPublicationTests(unittest.TestCase):
    def setUp(self):
        self.objects = sample()
        self.remote = {}
        self.creates = []

    def read(self, path):
        if path not in self.remote:
            raise FileNotFoundError(path)
        return self.remote[path]

    def create(self, path, raw):
        self.creates.append(path)
        if path in self.remote:
            raise RuntimeError('conditional write failed')
        self.remote[path] = raw

    def test_exact_publication_and_repeat_are_non_authorizing(self):
        first = publication.publish(self.objects, read=self.read, create=self.create)
        self.assertEqual(first['created'], 12)
        self.assertFalse(first['scope_activated'])
        self.assertFalse(first['gc_authorized'])
        self.creates.clear()
        second = publication.publish(self.objects, read=self.read, create=self.create)
        self.assertEqual(second['existing'], 12)
        self.assertEqual(self.creates, [])

    def test_conflict_or_bad_inventory_stops_before_first_write(self):
        key = next(iter(self.objects))
        self.remote[key] = b'foreign'
        with self.assertRaises(publication.Conflict):
            publication.publish(self.objects, read=self.read, create=self.create)
        self.assertEqual(self.creates, [])
        altered = dict(self.objects)
        altered[key] = b'changed'
        with self.assertRaises(ValueError):
            publication.publish(altered, read=self.read, create=self.create)
        self.assertEqual(self.creates, [])

    def test_ambiguous_write_accepted_only_if_exact_bytes_read_back(self):
        def uncertain(path, raw):
            self.creates.append(path)
            self.remote[path] = raw
            raise OSError('unknown response')
        result = publication.publish(self.objects, read=self.read, create=uncertain)
        self.assertEqual(result['reconciled'], 12)
        self.assertEqual(len(self.creates), 12)

    def test_ambiguous_missing_result_stops_without_retry(self):
        def lost(path, raw):
            self.creates.append(path)
            raise OSError('unknown response')
        with self.assertRaises(publication.Uncertain):
            publication.publish(self.objects, read=self.read, create=lost)
        self.assertEqual(len(self.creates), 1)

    def test_external_drift_blocks_final_readback(self):
        count = 0
        def read(path):
            nonlocal count
            count += 1
            if count > 24 and path == sorted(self.objects)[0]:
                return b'changed'
            return self.read(path)
        with self.assertRaises(publication.Conflict):
            publication.publish(self.objects, read=read, create=self.create)

    def test_semantic_validation_occurs_before_any_write(self):
        with patch.object(publication.intent, 'plan', side_effect=ValueError('invalid receipt')):
            with self.assertRaisesRegex(ValueError, 'invalid receipt'):
                publication.publish_candidate({}, {}, b'{}', read=self.read, create=self.create)
        self.assertEqual(self.creates, [])


if __name__ == '__main__':
    unittest.main()
