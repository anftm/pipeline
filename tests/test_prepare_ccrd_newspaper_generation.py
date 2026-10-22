"""Tiny local publication-plan fixtures; no remote or full-corpus operations."""
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import prepare_ccrd_newspaper_generation as planner


class PrepareNewspaperGenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.papers, self.merged = self.root / 'papers', self.root / 'merged'
        self.papers.mkdir()
        self.merged.mkdir()
        self.old_sha = 'a' * 64
        self.base = self.root / 'base.json'
        value = {'format': 1, 'tokenizer_version': planner.lifecycle.TOKENIZER, 'databases': {
            source: {'path': f'generations/ccrd-old/{source}.sqlite3', 'sha256': 'b' * 64,
                     'bytes': 1, 'documents': 1, 'fts_rows': 1} for source in ['CCRD', 'CW']}}
        self.base.write_text(json.dumps(value))
        sources = ['CCRD', 'CW', 'RMRB', 'PLA']
        payload = {'records': [{'source': s, 'doc_id': s + ':1'} for s in sources],
                   'sources': [{'slug': s, 'name': s, 'count': 1} for s in sources]}
        self.metadata = self.merged / 'data/search_data.json.gz'
        self.metadata.parent.mkdir()
        self.metadata.write_bytes(gzip.compress(json.dumps(payload).encode(), mtime=0))
        archives, databases = [], []
        for source in ['RMRB', 'PLA']:
            path = self.papers / 'data/fulltext' / (source + '.sqlite3')
            path.parent.mkdir(parents=True, exist_ok=True)
            identity = {'source': source, 'profile': planner.PROFILE, 'documents': 1, 'audit_documents': 1}
            with sqlite3.connect(path) as db:
                db.executescript('''
                    CREATE TABLE documents(doc_id TEXT PRIMARY KEY,summary TEXT);
                    CREATE TABLE texts(doc_id TEXT PRIMARY KEY,body TEXT);
                    CREATE TABLE postings(token_hash BLOB PRIMARY KEY,docs BLOB,doc_count INTEGER);
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE build_state(key TEXT PRIMARY KEY,value TEXT);
                    CREATE VIRTUAL TABLE content_fts USING fts5(content,tokenize='trigram');
                ''')
                db.executemany('INSERT INTO metadata VALUES(?,?)', [
                    ('tokenizer_version', planner.lifecycle.TOKENIZER), ('index_profile', planner.PROFILE),
                    ('build_complete', 'true')])
                db.executemany('INSERT INTO build_state VALUES(?,?)', [('phase', json.dumps('complete')),
                                                                         ('identity', json.dumps(identity))])
                db.execute('INSERT INTO documents VALUES(?,?)', (source + ':1', '完整正文'))
                db.execute('INSERT INTO texts VALUES(?,?)', (source + ':1', '完整正文'))
                db.execute("INSERT INTO content_fts(rowid,content) VALUES(1,'完整正文')")
            receipt = {'source': source, 'documents': 1, 'identity': identity, **planner.file_identity(path)}
            self.write(self.papers / 'receipts' / (source + '.json'), receipt)
            databases.append({key: receipt[key] for key in ['source', 'documents', 'sha256', 'bytes']})
            archive = self.papers / 'archives' / source / (source + '-1976.tar.gz')
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'fixture checksum-pinned archive')
            archives.append({'source': source, 'year': '1976', 'path': archive.relative_to(self.papers).as_posix(),
                             'identity': {'documents': 1}, **planner.file_identity(archive)})
        package = {'kind': 'newspaper-local-package', 'archives': archives, 'databases': databases}
        self.write(self.papers / 'package-receipt.json', package)
        self.write(self.papers / 'archives/manifest.json', {'archives': archives})
        merge = {'kind': 'ccrd-four-source-local-metadata', 'previous_base_metadata_sha256': self.old_sha,
                 'base_metadata_sha256': planner.file_identity(self.metadata)['sha256'],
                 'OCR_rebase_required': True, 'OCR_activation_copied': False, 'documents': 4,
                 'files': [{'path': 'data/search_data.json.gz', **planner.file_identity(self.metadata)}],
                 'newspaper_package_receipt_sha256': planner.file_identity(self.papers / 'package-receipt.json')['sha256']}
        self.write(self.merged / 'merge-receipt.json', merge)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def prepare(self, **kwargs):
        return planner.prepare(self.papers, self.merged, self.base, self.old_sha, **kwargs)

    def test_complete_plan_retains_old_indexes_and_hash_addressed_newspaper_bodies(self):
        result = self.prepare()
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(result['documents'], {'CCRD': 1, 'CW': 1, 'RMRB': 1, 'PLA': 1})
        self.assertTrue(result['complete_newspaper_audits'])
        self.assertEqual(result, self.prepare())
        self.assertEqual(result['manifest']['databases']['CCRD']['path'], 'generations/ccrd-old/CCRD.sqlite3')
        self.assertTrue(result['manifest']['databases']['RMRB']['path'].startswith('indexes/newspapers/RMRB/'))
        self.assertEqual(len(planner.lifecycle.references(result['manifest'], 'current.json')), 7)

    def test_sample_cannot_be_mistaken_for_complete_newspaper_corpus(self):
        path = self.papers / 'receipts/RMRB.json'
        receipt = json.loads(path.read_text())
        receipt['identity']['audit_documents'] = 2
        db_path = self.papers / 'data/fulltext/RMRB.sqlite3'
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE build_state SET value=? WHERE key='identity'", (json.dumps(receipt['identity']),))
        receipt.update(planner.file_identity(db_path))
        self.write(path, receipt)
        package_path = self.papers / 'package-receipt.json'
        package = json.loads(package_path.read_text())
        for ref in package['databases']:
            if ref['source'] == 'RMRB':
                ref.update({key: receipt[key] for key in ['sha256', 'bytes']})
        self.write(package_path, package)
        merge_path = self.merged / 'merge-receipt.json'
        merge = json.loads(merge_path.read_text())
        merge['newspaper_package_receipt_sha256'] = planner.file_identity(package_path)['sha256']
        self.write(merge_path, merge)
        with self.assertRaisesRegex(ValueError, 'sample newspapers'):
            self.prepare()
        self.assertTrue(self.prepare(allow_sample=True)['sample_only'])

    def test_wrong_base_and_corrupt_body_database_or_year_archive_block_planning(self):
        with self.assertRaisesRegex(ValueError, 'approved legacy base'):
            planner.prepare(self.papers, self.merged, self.base, 'f' * 64)
        archive = next((self.papers / 'archives/RMRB').glob('*.tar.gz'))
        archive.write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.prepare()


if __name__ == '__main__':
    unittest.main()
