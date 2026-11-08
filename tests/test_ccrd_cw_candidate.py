"""Local CW:0 candidate updates preserve every unrelated synthetic document."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import ccrd_cw_candidate as candidate
from scripts import ccrd_lifecycle, ccrd_postings
from scripts import ccrd_text_features as features

OLD = '原来的中国文字 and zero\n'
NEW = '更新后的中国文本 new word!\n'
OTHER = ['其他文章 abc 中国', 'unchanged 保持']


def make_database(path):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE documents(doc_id TEXT PRIMARY KEY,summary TEXT NOT NULL)')
        db.execute('CREATE TABLE postings(token_hash BLOB PRIMARY KEY,docs BLOB NOT NULL,doc_count INTEGER NOT NULL)')
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute('CREATE VIRTUAL TABLE content_fts USING fts5(content,tokenize="trigram",detail="full")')
        db.execute('CREATE TABLE snippet_anchors(token_hash BLOB NOT NULL,doc_number INTEGER NOT NULL, '
                   'char_offset INTEGER NOT NULL,PRIMARY KEY(token_hash,doc_number)) WITHOUT ROWID')
        db.execute('INSERT INTO metadata VALUES(?,?)', ('tokenizer_version', ccrd_lifecycle.TOKENIZER))
        numbers = {}
        for index, text in enumerate([OLD, *OTHER]):
            db.execute('INSERT INTO documents VALUES(?,?)', (f'CW:{index}', features.summarize(text)))
            db.execute('INSERT INTO content_fts(rowid,content) VALUES(?,?)', (index, features.normalized(text)))
            db.executemany('INSERT INTO snippet_anchors VALUES(?,?,?)',
                           ((key, index, offset) for key, offset in features.anchors(text).items()))
            for key in features.token_hashes(text): numbers.setdefault(key, []).append(index)
        db.executemany('INSERT INTO postings VALUES(?,?,?)',
                       ((key, ccrd_postings.encode(values), len(values)) for key, values in numbers.items()))


def identity(path):
    raw = path.read_bytes()
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def check_postings(db, texts):
    expected = {}
    for number, text in enumerate(texts):
        for key in features.token_hashes(text): expected.setdefault(key, []).append(number)
    actual = {key: ccrd_postings.decode(value, expected_count=count, maximum=len(texts))
              for key, value, count in db.execute('SELECT token_hash,docs,doc_count FROM postings')}
    if actual != expected:
        raise AssertionError('full posting rows differ')


class CcrdCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db, self.old, self.new, self.out = [self.root / n for n in ('original.sqlite', 'old.txt', 'new.txt', 'candidate.sqlite')]
        make_database(self.db); self.old.write_text(OLD); self.new.write_text(NEW)
        self.ids = {'source_identity': identity(self.db), 'old_identity': identity(self.old), 'new_identity': identity(self.new)}

    def build(self):
        return candidate.build(self.db, self.old, self.new, self.out, expected_documents=3, **self.ids)

    def test_only_cw0_changes_and_all_fixture_postings_match_full_rebuild_oracle(self):
        before = self.db.read_bytes()
        with sqlite3.connect(self.db) as db:
            old_others = list(db.execute('SELECT doc_id,summary FROM documents WHERE doc_id != ?', ('CW:0',)))
            old_fts = list(db.execute('SELECT rowid,content FROM content_fts WHERE rowid != 0 ORDER BY rowid'))
            old_anchors = list(db.execute('SELECT * FROM snippet_anchors WHERE doc_number != 0 ORDER BY token_hash,doc_number'))
        report = self.build()
        self.assertFalse(report['publication_ready']); self.assertEqual(report['remote_writes'], 0)
        self.assertEqual(report['non_target_audit']['non_target_rows']['documents'], 2)
        self.assertEqual(report['non_target_audit']['sqlite_integrity'], 'ok')
        self.assertEqual(report['candidate'], {'file': 'CW.sqlite3', **identity(self.out / 'CW.sqlite3')})
        self.assertEqual(self.db.read_bytes(), before)
        with sqlite3.connect(self.out / 'CW.sqlite3') as db:
            self.assertEqual(list(db.execute('SELECT doc_id,summary FROM documents WHERE doc_id != ?', ('CW:0',))), old_others)
            self.assertEqual(list(db.execute('SELECT rowid,content FROM content_fts WHERE rowid != 0 ORDER BY rowid')), old_fts)
            self.assertEqual(list(db.execute('SELECT * FROM snippet_anchors WHERE doc_number != 0 ORDER BY token_hash,doc_number')), old_anchors)
            self.assertEqual(db.execute('SELECT content FROM content_fts WHERE rowid=0').fetchone()[0], features.normalized(NEW))
            check_postings(db, [NEW, *OTHER])

    def test_bad_identity_old_text_corruption_or_document_count_never_keeps_candidate(self):
        self.ids['new_identity'] = {'bytes': len(NEW.encode()), 'sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'identity'): self.build()
        self.assertFalse(self.out.exists())
        self.ids['new_identity'] = identity(self.new)
        self.old.write_text('wrong old text'); self.ids['old_identity'] = identity(self.old)
        with self.assertRaisesRegex(ValueError, 'stored text differs'): self.build()
        self.assertFalse(self.out.exists())
        self.old.write_text(OLD); self.ids['old_identity'] = identity(self.old)
        with self.assertRaisesRegex(ValueError, 'counts differ'):
            candidate.build(self.db, self.old, self.new, self.out, expected_documents=4, **self.ids)
        self.assertFalse(self.out.exists())

    def test_old_anchors_or_postings_corruption_fails_closed(self):
        with sqlite3.connect(self.db) as db:
            db.execute('DELETE FROM snippet_anchors WHERE doc_number=0')
        self.ids['source_identity'] = identity(self.db)
        with self.assertRaisesRegex(ValueError, 'anchors differ'): self.build()
        self.assertFalse(self.out.exists())
        self.db.unlink(); make_database(self.db)
        with sqlite3.connect(self.db) as db:
            target = next(iter(features.token_hashes(OLD)))
            db.execute('DELETE FROM postings WHERE token_hash=?', (target,))
        self.ids['source_identity'] = identity(self.db)
        with self.assertRaisesRegex(ValueError, 'posting is absent'): self.build()
        self.assertFalse(self.out.exists())

    def test_output_collision_unchanged_source_and_failed_copy_preserve_inputs(self):
        self.out.write_bytes(b'pre-existing')
        with self.assertRaisesRegex(ValueError, 'new path'): self.build()
        self.assertEqual(self.out.read_bytes(), b'pre-existing'); self.out.unlink()
        self.new.write_text(OLD); self.ids['new_identity'] = identity(self.new)
        with self.assertRaisesRegex(ValueError, 'unchanged'): self.build()
        self.new.write_text(NEW); self.ids['new_identity'] = identity(self.new)
        with patch.object(candidate.shutil, 'copyfileobj', side_effect=OSError('copy interrupted')), \
                self.assertRaises(OSError):
            self.build()
        self.assertFalse(self.out.exists())
        self.assertEqual(identity(self.db), self.ids['source_identity'])

    def test_concurrent_output_creator_is_never_removed(self):
        original = Path.mkdir
        def race(path, *args, **kwargs):
            if path == self.out:
                original(self.out)
                (self.out / 'foreign').write_bytes(b'foreign')
                raise FileExistsError('another writer created output')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', race), self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual((self.out / 'foreign').read_bytes(), b'foreign')

    def test_non_target_tampering_is_detected_by_complete_audit(self):
        self.build(); path = self.out / 'CW.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute("UPDATE documents SET summary='corrupt' WHERE doc_id='CW:1'")
        with self.assertRaisesRegex(ValueError, 'non-target rows changed'):
            candidate.audit_unchanged(self.db, path, documents=3)
        with sqlite3.connect(path) as db:
            db.execute('UPDATE documents SET summary=? WHERE doc_id=?', (features.summarize(OTHER[0]), 'CW:1'))
            for key, raw, count in db.execute('SELECT token_hash,docs,doc_count FROM postings').fetchall():
                numbers = ccrd_postings.decode(raw, expected_count=count, maximum=3)
                if 1 in numbers:
                    remaining = [number for number in numbers if number != 1]
                    if remaining:
                        db.execute('UPDATE postings SET docs=?,doc_count=? WHERE token_hash=?',
                                   (ccrd_postings.encode(remaining), len(remaining), key))
                    else:
                        db.execute('DELETE FROM postings WHERE token_hash=?', (key,))
                    break
        with self.assertRaisesRegex(ValueError, 'posting membership changed'):
            candidate.audit_unchanged(self.db, path, documents=3)

    def test_invalid_utf8_and_unsafe_paths_are_rejected(self):
        self.new.write_bytes(b'\xff'); self.ids['new_identity'] = identity(self.new)
        with self.assertRaises(UnicodeDecodeError): self.build()
        self.assertFalse(self.out.exists())
        link = self.root / 'link'; link.symlink_to(self.db)
        with self.assertRaisesRegex(ValueError, 'non-symlink'):
            candidate.file_identity(link, candidate.MAX_DB_BYTES)


if __name__ == '__main__':
    unittest.main()
