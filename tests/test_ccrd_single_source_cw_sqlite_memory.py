"""Exercise target replacement in a real in-memory SQLite FTS fixture only.

This is a test of the shadow rules, not a reusable SQLite production writer.
It does not open paths, connect to remote storage or authorize publication.
"""
import hashlib
import re
import sqlite3
import unittest

from scripts import ccrd_single_source_cw_shadow as shadow


class CwSqliteMemoryTests(unittest.TestCase):
    def make_db(self, documents):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript('''
            CREATE TABLE documents (doc_id TEXT PRIMARY KEY, summary TEXT NOT NULL);
            CREATE TABLE postings (token_hash BLOB PRIMARY KEY, docs BLOB NOT NULL, doc_count INTEGER NOT NULL);
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES('tokenizer_version','cjk-bigram-boundary-fts5-v6-snippet-anchors');
            CREATE VIRTUAL TABLE content_fts USING fts5(content, tokenize='trigram', detail='full');
            CREATE TABLE snippet_anchors (token_hash BLOB NOT NULL, doc_number INTEGER NOT NULL, char_offset INTEGER NOT NULL, PRIMARY KEY (token_hash, doc_number)) WITHOUT ROWID;
            CREATE INDEX idx_documents_doc_id ON documents(doc_id);
        ''')
        built = shadow.build(documents)
        connection.executemany('INSERT INTO documents VALUES(?,?)', built['documents'].items())
        connection.executemany('INSERT INTO content_fts(rowid,content) VALUES(?,?)', built['content_fts'].items())
        connection.executemany('INSERT INTO snippet_anchors VALUES(?,?,?)',
                               ((token, number, offset) for number, values in built['snippet_anchors'].items()
                                for token, offset in values.items()))
        connection.executemany('INSERT INTO postings VALUES(?,?,?)',
                               ((token, raw, len(shadow.decode_doc_ids(raw)))
                                for token, raw in built['postings'].items()))
        connection.commit()
        return connection

    def observation(self, connection):
        documents = dict(connection.execute('SELECT doc_id,summary FROM documents'))
        fulltext = dict(connection.execute('SELECT rowid,content FROM content_fts'))
        anchors = {number: {} for number in fulltext}
        for token, number, offset in connection.execute('SELECT token_hash,doc_number,char_offset FROM snippet_anchors'):
            anchors[number][token] = offset
        posting_rows = list(connection.execute('SELECT token_hash,docs,doc_count FROM postings'))
        for _token, raw, count in posting_rows:
            self.assertEqual(len(shadow.decode_doc_ids(raw)), count)
        postings = {token: raw for token, raw, _count in posting_rows}
        return {'documents': documents, 'content_fts': fulltext, 'snippet_anchors': anchors,
                'postings': postings, 'publication_ready': False, 'remote_writes': 0}

    def update_test_transaction(self, connection, old_text, new_text, *, fail_after=None):
        """Fixture-only SQL, with deliberately injectable transaction failures."""
        before = self.observation(connection)
        expected = shadow.replace_target(before, old_text, new_text, expected_ids=set(before['documents']))
        current = shadow.token_hashes(old_text)
        new = shadow.token_hashes(new_text)

        def fault(name):
            if fail_after == name:
                raise RuntimeError('synthetic interruption')

        with connection:
            connection.execute('UPDATE documents SET summary=? WHERE doc_id=?',
                               (re.sub(r'\s+', ' ', new_text).strip()[:700], 'CW:0'))
            fault('summary')
            connection.execute('DELETE FROM content_fts WHERE rowid=0')
            connection.execute('INSERT INTO content_fts(rowid,content) VALUES(0,?)', (shadow.fts_content(new_text),))
            fault('fts')
            connection.execute('DELETE FROM snippet_anchors WHERE doc_number=0')
            connection.executemany('INSERT INTO snippet_anchors VALUES(?,0,?)', shadow.snippet_anchors(new_text).items())
            fault('anchors')
            for token in current | new:
                row = connection.execute('SELECT docs,doc_count FROM postings WHERE token_hash=?', (token,)).fetchone()
                values = set(shadow.decode_doc_ids(row[0])) if row else set()
                if row and row[1] != len(values):
                    raise ValueError('fixture posting count changed')
                values.discard(0)
                if token in new:
                    values.add(0)
                if values:
                    encoded = shadow.encode_doc_ids(values)
                    connection.execute('INSERT OR REPLACE INTO postings VALUES(?,?,?)', (token, encoded, len(values)))
                else:
                    connection.execute('DELETE FROM postings WHERE token_hash=?', (token,))
            fault('postings')
            self.assertEqual(self.observation(connection), expected)
        return expected

    def test_real_sqlite_replacement_matches_full_rebuild_and_preserves_old_index(self):
        old = {'CW:0': '旧目标句子 hello', 'CW:1': '其他句子 hello', 'CW:128': '另一个来源 文本'}
        new = '新目标句子 DIFFERENT'
        source = self.make_db(old)
        candidate = sqlite3.connect(':memory:')
        self.addCleanup(candidate.close)
        source.backup(candidate)
        before = self.observation(source)
        updated = self.update_test_transaction(candidate, old['CW:0'], new)
        rebuilt = self.make_db({**old, 'CW:0': new})
        self.assertEqual(updated, self.observation(rebuilt))
        self.assertEqual(self.observation(source), before)
        self.assertEqual(candidate.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        self.assertEqual(candidate.execute("SELECT rowid FROM content_fts WHERE content_fts MATCH '新目标'").fetchall(), [(0,)])
        self.assertEqual(candidate.execute("SELECT rowid FROM content_fts WHERE content_fts MATCH '旧目标'").fetchall(), [])
        self.assertEqual(candidate.execute("SELECT rowid FROM content_fts WHERE content_fts MATCH '其他句'").fetchall(), [(1,)])
        self.assertFalse(updated['publication_ready'])

    def test_failures_roll_back_each_fixture_stage_and_preserve_source(self):
        docs = {'CW:0': '旧正文句子', 'CW:1': '其他人的原文', 'CW:128': '保留字节'}
        source = self.make_db(docs)
        expected = self.observation(source)
        for stage in ('summary', 'fts', 'anchors', 'postings'):
            with self.subTest(stage=stage):
                candidate = sqlite3.connect(':memory:')
                self.addCleanup(candidate.close)
                source.backup(candidate)
                with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
                    self.update_test_transaction(candidate, docs['CW:0'], '新正文句子', fail_after=stage)
                self.assertEqual(self.observation(candidate), expected)
                self.assertEqual(self.observation(source), expected)

    def test_sqlite_commit_then_crash_does_not_make_external_publication_atomic(self):
        docs = {'CW:0': '旧的句子', 'CW:1': '其他句子'}
        source = self.make_db(docs)
        candidate = sqlite3.connect(':memory:')
        self.addCleanup(candidate.close)
        source.backup(candidate)
        external_state = {'space': 'old', 'current_manifest': 'old', 'receipt': None}
        updated = self.update_test_transaction(candidate, docs['CW:0'], '新的句子')
        # The fixture simulates process interruption immediately after the local
        # transaction commits and before any external pointer/receipt changes.
        self.assertEqual(self.observation(candidate), updated)
        self.assertEqual(external_state, {'space': 'old', 'current_manifest': 'old', 'receipt': None})
        self.assertNotEqual(self.observation(candidate), self.observation(source))

    def test_corrupted_postings_abort_before_any_update(self):
        docs = {'CW:0': '旧正文猫', 'CW:1': '其他猫'}
        connection = self.make_db(docs)
        token = hashlib.sha256('猫'.encode()).digest()[:16]
        connection.execute('UPDATE postings SET docs=? WHERE token_hash=?', (b'\x80', token))
        connection.commit()
        before = list(connection.execute('SELECT doc_id,summary FROM documents ORDER BY doc_id'))
        with self.assertRaises(ValueError):
            self.update_test_transaction(connection, docs['CW:0'], '新正文猫')
        self.assertEqual(list(connection.execute('SELECT doc_id,summary FROM documents ORDER BY doc_id')), before)


if __name__ == '__main__':
    unittest.main()
