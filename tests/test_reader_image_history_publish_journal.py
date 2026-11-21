import unittest
from dataclasses import replace

from scripts import reader_image_history_publish as publication
from scripts import reader_image_history_publish_journal as journal
from scripts.reader_image_history_store import WriteUnconfirmed
from tests.test_reader_image_history_store import MemoryS3


class LegacyJournalTests(unittest.TestCase):
    def setUp(self):
        self.key = 'a' * 64
        self.client = MemoryS3()
        self.journal = journal.LegacyJournal(self.client)
        self.first = publication.Record('b' * 64, 'catalog-permitted')
        self.second = replace(self.first, state='sidecar-permitted', catalog_etag='"c"')
        self.last = replace(self.second, state='legacy-observed', sidecar_etag='"s"')

    def test_uses_exact_claim_and_cas_with_fresh_readback(self):
        self.assertIsNone(self.journal.read(self.key))
        self.assertTrue(self.journal.claim(self.key, self.first))
        self.assertFalse(self.journal.claim(self.key, self.first))
        self.assertTrue(self.journal.advance(self.key, self.first, self.second))
        self.assertFalse(self.journal.advance(self.key, self.first, self.second))
        self.assertTrue(self.journal.advance(self.key, self.second, self.last))
        self.assertEqual(self.journal.read(self.key), self.last)
        self.assertEqual([condition for _, condition in self.client.puts],
                         [{'IfNoneMatch': '*'}, {'IfNoneMatch': '*'}, {'IfMatch': '"put-1"'}, {'IfMatch': '"put-3"'}])

    def test_unconfirmed_advance_never_confers_a_permit(self):
        self.assertTrue(self.journal.claim(self.key, self.first))
        self.client.fault = 'after'
        with self.assertRaises(WriteUnconfirmed):
            self.journal.advance(self.key, self.first, self.second)
        self.assertEqual(self.journal.read(self.key), self.second)
        old = list(self.client.puts)
        self.assertFalse(self.journal.advance(self.key, self.first, self.second))
        self.assertEqual(self.client.puts, old)

    def test_malformed_oversized_and_changed_catalog_etag_fail_closed(self):
        path = journal._path(self.key)
        for raw in (b'{}', b' ' * (journal.MAX_RECORD + 1)):
            self.client.objects[path] = raw, '"bad"'
            with self.assertRaises(ValueError):
                self.journal.read(self.key)
        with self.assertRaises(ValueError):
            self.journal.advance(self.key, self.second, replace(self.last, catalog_etag='"foreign"'))
        self.assertFalse(self.client.puts)
