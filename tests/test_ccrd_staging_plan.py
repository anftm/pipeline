"""A four-source staging description preserves references and cannot publish."""
import copy
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from scripts import ccrd_cw_candidate, ccrd_lifecycle, ccrd_postings, ccrd_text_features
from scripts import ccrd_staging_plan as stage
from tests.test_ccrd_candidate_assembly import CandidateAssemblyTests
from tests.test_ccrd_candidate_preflight import identity
from tests.test_ccrd_cw_candidate import OTHER
from tests.test_ccrd_metadata_patch import NEW, OLD


class StagingPlanTests(CandidateAssemblyTests):
    def description(self):
        with patch.object(stage.assembly, '_cw_database',
                          return_value=({'sha256': 'c' * 64, 'bytes': 42}, {'seconds': 0.1})):
            return self.make()

    def test_full_four_source_manifest_and_all_copy_intents(self):
        description = self.description()
        result = stage.make(self.pointer, description)
        self.assertFalse(result['publication_ready']); self.assertEqual(result['remote_writes'], 0)
        manifest = result['manifest']; refs = ccrd_lifecycle.references(manifest, 'current.json')
        self.assertEqual(set(manifest['databases']), {'CCRD', 'CW', 'RMRB', 'PLA'})
        self.assertEqual(len(manifest['metadata']), 17)
        self.assertEqual(len(manifest['archives']), len(self.manifest['archives']))
        self.assertEqual(len(refs), 4 + 17 + len(self.manifest['archives']))
        self.assertEqual(len(result['objects']), 17 + len(self.manifest['archives']) + 1)
        self.assertEqual(manifest['databases']['CCRD'], self.manifest['databases']['CCRD'])
        self.assertEqual(manifest['databases']['RMRB'], self.manifest['databases']['RMRB'])
        self.assertEqual(stage.make(self.pointer, description)['generation'], result['generation'])

    def test_real_sqlite_and_metadata_candidate_to_full_staging_plan(self):
        old_path, new_path = self.root / 'old.txt', self.root / 'new.txt'
        # Rebuild a valid three-document source using the assembly fixture text.
        source_db = self.root / 'real-source.sqlite'
        with sqlite3.connect(source_db) as db:
            db.execute('CREATE TABLE documents(doc_id TEXT PRIMARY KEY,summary TEXT NOT NULL)')
            db.execute('CREATE TABLE postings(token_hash BLOB PRIMARY KEY,docs BLOB NOT NULL,doc_count INTEGER NOT NULL)')
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE VIRTUAL TABLE content_fts USING fts5(content,tokenize="trigram",detail="full")')
            db.execute('CREATE TABLE snippet_anchors(token_hash BLOB NOT NULL,doc_number INTEGER NOT NULL, '
                       'char_offset INTEGER NOT NULL,PRIMARY KEY(token_hash,doc_number)) WITHOUT ROWID')
            db.execute('INSERT INTO metadata VALUES(?,?)', ('tokenizer_version', ccrd_lifecycle.TOKENIZER))
            entries = {}
            for number, text in enumerate([OLD, *OTHER]):
                db.execute('INSERT INTO documents VALUES(?,?)', (f'CW:{number}', ccrd_text_features.summarize(text)))
                db.execute('INSERT INTO content_fts(rowid,content) VALUES(?,?)', (number, ccrd_text_features.normalized(text)))
                db.executemany('INSERT INTO snippet_anchors VALUES(?,?,?)',
                               ((key, number, offset) for key, offset in ccrd_text_features.anchors(text).items()))
                for key in ccrd_text_features.token_hashes(text): entries.setdefault(key, []).append(number)
            db.executemany('INSERT INTO postings VALUES(?,?,?)',
                           ((key, ccrd_postings.encode(values), len(values)) for key, values in entries.items()))
        old_path.write_text(OLD); new_path.write_text(NEW)
        self.manifest['databases']['CW'].update({'bytes': source_db.stat().st_size,
                                                 'sha256': hashlib.sha256(source_db.read_bytes()).hexdigest()})
        pointer = json.dumps(self.manifest, ensure_ascii=False).encode(); pointer_sha = hashlib.sha256(pointer).hexdigest()
        out = self.root / 'sqlite-candidate'
        ccrd_cw_candidate.build(source_db, old_path, new_path, out,
                                source_identity=identity(source_db.read_bytes()), old_identity=identity(old_path.read_bytes()),
                                new_identity=identity(new_path.read_bytes()), expected_documents=3)
        description = stage.assembly.assemble(pointer, OLD.encode(), NEW.encode(), self.observed, self.source_observation,
                                               approved_pointer_sha256=pointer_sha, parent_metadata=self.parent,
                                               candidate_metadata=self.output, parent_cw=source_db,
                                               candidate_cw=out / 'CW.sqlite3')
        result = stage.make(pointer, description)
        self.assertEqual(result['manifest']['databases']['CW']['sha256'], description['cw_database']['sha256'])
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)

    def test_modified_description_missing_or_changed_parent_is_rejected(self):
        description = self.description()
        changed = copy.deepcopy(description); changed['new_text']['bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'description identity'):
            stage.make(self.pointer, changed)
        with self.assertRaises(ValueError):
            stage.make(self.pointer + b' ', description)
        changed = copy.deepcopy(description); changed['publication_ready'] = True
        with self.assertRaises(ValueError):
            stage.make(self.pointer, changed)


if __name__ == '__main__':
    unittest.main()
