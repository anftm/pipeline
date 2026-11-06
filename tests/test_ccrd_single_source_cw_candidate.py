"""Test the offline byte-to-byte CW candidate on synthetic SQLite only."""

from copy import deepcopy
import base64
import json
import os
import random
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from scripts import ccrd_single_source_cw_candidate as candidate
from scripts import ccrd_single_source_cw_shadow as shadow
from scripts import ccrd_single_source_plan as planner
from tests import test_ccrd_single_source_cw_sqlite_memory as sqlite_fixture
from tests import test_ccrd_single_source_plan as plan_fixture


@unittest.skipUnless(hasattr(sqlite3.Connection, 'serialize') and hasattr(sqlite3.Connection, 'deserialize'),
                     'SQLite byte serialization requires a supported Python build')
class CwCandidateTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(sqlite3.Connection, 'serialize'):
            return
        self.plan_fixture = plan_fixture.SingleSourcePlanTests('test_identical_txt_is_noop_even_if_input_gzip_is_not_canonical')
        self.plan_fixture.setUp()
        self.sqlite_fixture = sqlite_fixture.CwSqliteMemoryTests('test_corrupted_postings_abort_before_any_update')
        self.sqlite_fixture.setUp()
        self.addCleanup(self.sqlite_fixture.doCleanups)
        self.documents = {'CW:0': self.plan_fixture.current.decode(), 'CW:1': '保留文档 second',
                          'CW:128': '共享 内容 token'}

    def args(self, *, change=None):
        base = self.sqlite_fixture.make_db(self.documents)
        if change is not None:
            change(base)
            base.commit()
        raw = base.serialize()
        metadata = deepcopy(self.plan_fixture.metadata)
        metadata['sources'][1]['count'] = 3
        metadata['records'].append({'source': 'CW', 'doc_id': 'CW:128', 'size': 1})
        pointer = deepcopy(self.plan_fixture.pointer)
        pointer['databases']['CW'].update({'documents': 3, 'fts_rows': 3, 'sha256': planner.sha(raw),
                                          'bytes': len(raw)})
        pointer['builder'] = {'sha256': next(iter(candidate.APPROVED_BUILDERS))}
        pointer['tokenizer_version'] = candidate.TOKENIZER
        args = self.plan_fixture.arguments(metadata=metadata, pointer=pointer)
        args['database_bytes'] = raw
        return args

    def open_bytes(self, raw):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.deserialize(raw)
        return db

    def test_candidate_matches_rebuilt_semantics_and_preserves_input(self):
        args = self.args()
        old = args['database_bytes']
        result = candidate.build_candidate(**args)
        db = self.open_bytes(result['candidate_bytes'])
        expected = shadow.build({**self.documents, 'CW:0': args['next_txt'].decode()})
        self.assertEqual(self.sqlite_fixture.observation(db), expected)
        self.assertEqual(args['database_bytes'], old)
        self.assertTrue(result['changed'])
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(result['candidate_sha256'], planner.sha(result['candidate_bytes']))
        self.assertEqual(result['counts'], {
            name: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for name, table in (('documents', 'documents'), ('fts_rows', 'content_fts'),
                                ('postings', 'postings'), ('snippet_anchors', 'snippet_anchors'))})
        self.assertEqual(result['plan']['unchanged_database_sha256'],
                         {name: self.plan_fixture.databases[name]['sha256'] for name in ('CCRD', 'RMRB', 'PLA')})
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))

    def test_noop_returns_exact_original_bytes(self):
        args = self.args()
        args['next_txt'] = args['current_txt']
        result = candidate.build_candidate(**args)
        self.assertFalse(result['changed'])
        self.assertEqual(result['candidate_bytes'], args['database_bytes'])
        self.assertFalse(result['publication_ready'])

    def test_mismatched_identity_or_inputs_fail_closed(self):
        for mode in ('wrong-database', 'wrong-current', 'wrong-pointer', 'wrong-builder', 'wrong-size'):
            args = self.args()
            if mode == 'wrong-database':
                args['database_bytes'] += b'changed'
            elif mode == 'wrong-current':
                args['current_txt'] = b'not the current target'
            elif mode == 'wrong-pointer':
                args['activation_bytes'] = b'{}'
            else:
                pointer = json.loads(args['pointer_bytes'])
                if mode == 'wrong-builder':
                    pointer['builder']['sha256'] = 'b' * 64
                else:
                    pointer['databases']['CW']['bytes'] += 1
                args['pointer_bytes'] = plan_fixture.encode(pointer)
                activation = json.loads(args['activation_bytes'])
                activation['sha256'] = planner.sha(args['pointer_bytes'])
                args['activation_bytes'] = plan_fixture.encode(activation)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                candidate.build_candidate(**args)

    def test_corrupt_fts_postings_extra_schema_or_target_are_rejected(self):
        def tamper_fts(db):
            db.execute("UPDATE content_fts_content SET c0='tampered' WHERE id=0")

        def tamper_postings(db):
            db.execute("UPDATE postings SET docs=X'80' WHERE token_hash=(SELECT token_hash FROM postings LIMIT 1)")

        def extra_table(db):
            db.execute('CREATE TABLE unexpected(value TEXT)')

        def wrong_body(db):
            db.execute("UPDATE documents SET summary='tampered' WHERE doc_id='CW:0'")

        def extra_document(db):
            db.execute("INSERT INTO documents VALUES('CW:999','unexpected')")

        def wrong_posting_count(db):
            db.execute('UPDATE postings SET doc_count=doc_count+1 WHERE token_hash=(SELECT token_hash FROM postings LIMIT 1)')

        def unknown_posting_document(db):
            db.execute('UPDATE postings SET docs=?,doc_count=1 WHERE token_hash=(SELECT token_hash FROM postings LIMIT 1)',
                       (shadow.encode_doc_ids([999]),))

        def mismatched_fts_index(db):
            db.execute("UPDATE content_fts_content SET c0='tampered non-target text' WHERE id=1")

        def wrong_tokenizer(db):
            db.execute("UPDATE metadata SET value='wrong-tokenizer' WHERE key='tokenizer_version'")

        def extra_trigger(db):
            db.execute('CREATE TRIGGER unexpected AFTER UPDATE ON documents BEGIN SELECT 1; END')

        for change in (tamper_fts, tamper_postings, extra_table, wrong_body, extra_document, wrong_posting_count,
                       unknown_posting_document, mismatched_fts_index, wrong_tokenizer, extra_trigger):
            with self.subTest(change=change.__name__), self.assertRaises(ValueError):
                candidate.build_candidate(**self.args(change=change))

    def test_altered_unique_indexes_and_table_constraints_are_rejected(self):
        def recreate_postings(db, *, token_primary=True, count_unique=False):
            rows = db.execute('SELECT token_hash,docs,doc_count FROM postings').fetchall()
            db.execute('DROP TABLE postings')
            primary = 'PRIMARY KEY' if token_primary else ''
            unique = 'UNIQUE' if count_unique else ''
            db.execute(f'CREATE TABLE postings (token_hash BLOB {primary}, docs BLOB NOT NULL, '
                       f'doc_count INTEGER NOT NULL {unique})')
            if not count_unique:
                db.executemany('INSERT INTO postings VALUES(?,?,?)', rows)

        def drop_posting_primary(db):
            recreate_postings(db, token_primary=False)

        def add_posting_count_uniqueness(db):
            recreate_postings(db, count_unique=True)

        def extra_explicit_index(db):
            db.execute('CREATE INDEX unrelated_summary ON documents(summary)')

        def missing_explicit_index(db):
            db.execute('DROP INDEX idx_documents_doc_id')

        for change in (drop_posting_primary, add_posting_count_uniqueness, extra_explicit_index,
                       missing_explicit_index):
            with self.subTest(change=change.__name__), self.assertRaisesRegex(ValueError, 'SQLite (tables|indexes)'):
                candidate.build_candidate(**self.args(change=change))

    def test_unknown_valid_base_postings_are_not_repaired_or_silently_removed(self):
        """Limit: structural checks cannot prove other source bodies are correct."""
        def add_wrong_but_valid_posting(db):
            db.execute('INSERT INTO postings VALUES(?,?,?)', (b'Z' * 16, shadow.encode_doc_ids([1]), 1))

        args = self.args(change=add_wrong_but_valid_posting)
        result = candidate.build_candidate(**args)
        db = self.open_bytes(result['candidate_bytes'])
        self.assertEqual(db.execute('SELECT docs,doc_count FROM postings WHERE token_hash=?', (b'Z' * 16,)).fetchone(),
                         (shadow.encode_doc_ids([1]), 1))
        self.assertFalse(result['publication_ready'])

    def test_synthetic_random_changes_match_complete_shadow_rebuild(self):
        randomizer = random.Random(963)
        alphabet = '猫 狗甲 abcABC09！。\nＡ'
        args = self.args()
        for _ in range(30):
            args['next_txt'] = ''.join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(1, 55))).encode()
            with self.subTest(next_txt=args['next_txt']):
                result = candidate.build_candidate(**args)
                expected = shadow.build({**self.documents, 'CW:0': args['next_txt'].decode()})
                self.assertEqual(self.sqlite_fixture.observation(self.open_bytes(result['candidate_bytes'])), expected)

    def test_sql_or_postcheck_failure_returns_no_candidate(self):
        args = self.args()
        original = args['database_bytes']
        for fn in ('_posting', '_verify_postings'):
            with self.subTest(fn=fn), patch.object(candidate, fn, side_effect=RuntimeError('synthetic failure')):
                with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                    candidate.build_candidate(**args)
            self.assertEqual(args['database_bytes'], original)
        db = self.open_bytes(original)
        self.assertEqual(db.execute("SELECT summary FROM documents WHERE doc_id='CW:0'").fetchone(),
                         (self.documents['CW:0'],))

    def test_unsupported_python_fails_without_producing_candidate(self):
        # The system Python in the harness lacks serialize/deserialize. Test the
        # explicit capability gate without touching any other implementation.
        args = self.args()
        with patch.object(candidate.sqlite3, 'Connection', object):
            with self.assertRaisesRegex(RuntimeError, 'serialize/deserialize'):
                candidate.build_candidate(**args)

    def test_candidate_bytes_are_stable_across_hash_seeds_with_the_same_sqlite(self):
        args = self.args()
        encoded = {key: base64.b64encode(value).decode() if isinstance(value, bytes) else value
                   for key, value in args.items()}
        command = (
            "import base64,json,sys; from scripts.ccrd_single_source_cw_candidate import build_candidate; "
            "args=json.load(sys.stdin); args={k:(v if k=='space_revision' else base64.b64decode(v)) "
            "for k,v in args.items()}; print(build_candidate(**args)['candidate_sha256'])"
        )
        digests = []
        for seed in ('1', '2', '893'):
            completed = subprocess.run([sys.executable, '-B', '-c', command], input=json.dumps(encoded),
                                       env={**os.environ, 'PYTHONHASHSEED': seed}, text=True,
                                       capture_output=True, timeout=30, check=True)
            digests.append(completed.stdout.strip())
        self.assertEqual(len(set(digests)), 1, digests)

    def test_parallel_candidates_do_not_share_mutable_state(self):
        args = self.args()
        originals = (args['database_bytes'], args['current_txt'])
        variants = [f'并发合成变化 {number}'.encode() for number in range(8)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda txt: candidate.build_candidate(**{**args, 'next_txt': txt}), variants))
        for text, result in zip(variants, results):
            expected = shadow.build({**self.documents, 'CW:0': text.decode()})
            self.assertEqual(self.sqlite_fixture.observation(self.open_bytes(result['candidate_bytes'])), expected)
            self.assertFalse(result['publication_ready'])
        self.assertEqual((args['database_bytes'], args['current_txt']), originals)


if __name__ == '__main__':
    unittest.main()
