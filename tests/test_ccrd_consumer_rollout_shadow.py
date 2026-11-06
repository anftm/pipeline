"""Reproduce current per-file replacement vs. independently refreshed reader.

The reader is a tiny model of the fixed Space revision: readiness checks four
loaded indexes' tokenizers and document counts, while bucket status is read
separately. This is not a live Space test or a proposed deployable change.
"""
from dataclasses import dataclass
import unittest


SOURCES = ('CCRD', 'CW', 'RMRB', 'PLA')


@dataclass(frozen=True)
class Db:
    revision: str
    count: int = 10
    tokenizer: str = 'supported'


class LiveModel:
    def __init__(self):
        self.files = {source: Db('old') for source in SOURCES}
        self.loaded = dict(self.files)
        self.status = {'state': 'checking', 'generation': None}

    def copy_complete_and_replace_one(self, source, db):
        # All candidate bytes and counts have been checked before replacement;
        # no claim about the real transport or object-store writers is made.
        self.files[source] = db

    def refresh(self):
        # Models an independent reader seeing whichever file names are visible
        # at the instant it refreshes, without a cross-source generation fence.
        self.loaded.update(self.files)

    def endpoint(self):
        ready = all(self.loaded.get(source) and self.loaded[source].count == 10
                    and self.loaded[source].tokenizer == 'supported' for source in SOURCES)
        return {'ready': ready, 'bucket': dict(self.status),
                'loaded_revisions': {source: self.loaded[source].revision for source in SOURCES}}


class ConsumerRolloutShadowTests(unittest.TestCase):
    def test_ready_flag_alone_can_allow_new_cw_among_old_sources(self):
        live = LiveModel()
        live.copy_complete_and_replace_one('CW', Db('new'))
        live.refresh()
        status = live.endpoint()
        self.assertTrue(status['ready'])
        self.assertEqual(status['bucket']['state'], 'checking')
        self.assertEqual(status['loaded_revisions'], {'CCRD': 'old', 'CW': 'new', 'RMRB': 'old', 'PLA': 'old'})
        # It is *not* a receipt for a coherent four-source activation. The
        # actual new single-TXT draft reuses non-CW bytes; this test illustrates
        # identity uncertainty, not necessarily a user-visible data mismatch.
        self.assertNotEqual(set(status['loaded_revisions'].values()), {'new'})

    def test_writer_crashes_after_all_local_files_before_status_ready(self):
        live = LiveModel()
        for source in SOURCES:
            live.copy_complete_and_replace_one(source, Db('new'))
        live.refresh()
        status = live.endpoint()
        self.assertTrue(status['ready'])
        self.assertEqual(set(status['loaded_revisions'].values()), {'new'})
        self.assertEqual(status['bucket'], {'state': 'checking', 'generation': None})
        # New bytes do not prove that the status/journal was durably committed.

    def test_status_and_file_identities_must_both_be_checked_by_any_future_protocol(self):
        live = LiveModel()
        live.copy_complete_and_replace_one('CW', Db('new'))
        live.status = {'state': 'ready', 'generation': 'new'}  # forged or stale state
        live.refresh()
        status = live.endpoint()
        self.assertTrue(status['ready'])
        self.assertEqual(status['bucket']['generation'], 'new')
        self.assertEqual(status['loaded_revisions']['RMRB'], 'old')
        # A status label plus counts is not an exact byte identity receipt.


if __name__ == '__main__':
    unittest.main()
