"""Construct a CW SQLite candidate from supplied bytes, entirely offline.

This is NOT a publisher or production-ready updater. It has no file, network,
bucket, workflow, OCR or activation entrypoint. A caller must separately pin
the inputs, validate all consumers/caches, isolate writers, and coordinate a
recoverable publication. Candidate results always say publication_ready=False.

Requires Python with sqlite3 Connection.deserialize/serialize and FTS5 trigram.
Input and output are held in memory; production-size performance is untested.
It validates the target and posting structure, not the correctness of every
other document's original body or all non-target posting memberships.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3

try:
    from . import ccrd_single_source_cw_shadow as shadow, ccrd_single_source_plan as planner
    from .ccrd_strict_json import loads as strict_json_loads
except ImportError:
    import ccrd_single_source_cw_shadow as shadow
    import ccrd_single_source_plan as planner
    from ccrd_strict_json import loads as strict_json_loads


TOKENIZER = 'cjk-bigram-boundary-fts5-v6-snippet-anchors'
APPROVED_BUILDERS = {'ed3cfbb2af5871c8a5d368512e939aa4164aebd71f43e1ff9af6cde3e0a02fac'}
SQLITE_SCHEMA = {
    'documents': 'CREATE TABLE documents (doc_id TEXT PRIMARY KEY, summary TEXT NOT NULL)',
    'postings': 'CREATE TABLE postings (token_hash BLOB PRIMARY KEY, docs BLOB NOT NULL, doc_count INTEGER NOT NULL)',
    'metadata': 'CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'content_fts': "CREATE VIRTUAL TABLE content_fts USING fts5(content, tokenize='trigram', detail='full')",
    'content_fts_data': "CREATE TABLE 'content_fts_data'(id INTEGER PRIMARY KEY, block BLOB)",
    'content_fts_idx': "CREATE TABLE 'content_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID",
    'content_fts_content': "CREATE TABLE 'content_fts_content'(id INTEGER PRIMARY KEY, c0)",
    'content_fts_docsize': "CREATE TABLE 'content_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)",
    'content_fts_config': "CREATE TABLE 'content_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID",
    'snippet_anchors': ('CREATE TABLE snippet_anchors (token_hash BLOB NOT NULL, doc_number INTEGER NOT NULL, '
                        'char_offset INTEGER NOT NULL, PRIMARY KEY (token_hash, doc_number)) WITHOUT ROWID'),
}
EXPLICIT_INDEX = {'idx_documents_doc_id': 'CREATE INDEX idx_documents_doc_id ON documents(doc_id)'}


def _schema(connection):
    tables = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
    if dict(tables) != SQLITE_SCHEMA:
        raise ValueError('SQLite tables differ from the reviewed historical builder')
    if connection.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger','view')").fetchall():
        raise ValueError('triggers or views are not supported')
    indexes = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='index'").fetchall()
    expected_indexes = {**EXPLICIT_INDEX, 'sqlite_autoindex_documents_1': None,
                        'sqlite_autoindex_postings_1': None, 'sqlite_autoindex_metadata_1': None}
    if dict(indexes) != expected_indexes:
        raise ValueError('SQLite indexes differ from the reviewed historical builder')
    databases = connection.execute('PRAGMA database_list').fetchall()
    if (not databases or databases[0] != (0, 'main', '')
            or any(name not in {'main', 'temp'} or path for _number, name, path in databases)
            or connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]):
        raise ValueError('SQLite attached database or integrity failure')
    columns = {
        'documents': ['doc_id', 'summary'],
        'postings': ['token_hash', 'docs', 'doc_count'],
        'metadata': ['key', 'value'],
        'content_fts': ['content'],
        'snippet_anchors': ['token_hash', 'doc_number', 'char_offset'],
    }
    for table, expected_columns in columns.items():
        if [row[1] for row in connection.execute(f'PRAGMA table_info({table})')] != expected_columns:
            raise ValueError('unexpected SQLite column layout: ' + table)
    if {row[1] for row in connection.execute('PRAGMA table_xinfo(content_fts)') if row[6]} != {'content_fts', 'rank'}:
        raise ValueError('unexpected FTS hidden columns')
    sql = dict(tables)['content_fts'] or ''
    if not (re.search(r'\bUSING\s+fts5\s*\(', sql, re.I)
            and re.search(r"\btokenize\s*=\s*['\"]trigram['\"]", sql, re.I)
            and re.search(r"\bdetail\s*=\s*['\"]full['\"]", sql, re.I)):
        raise ValueError('unsupported FTS schema')
    if connection.execute("SELECT key,value FROM metadata ORDER BY key").fetchall() != [('tokenizer_version', TOKENIZER)]:
        raise ValueError('unexpected CW tokenizer metadata')


def _posting(connection, token, valid_numbers):
    row = connection.execute('SELECT docs,doc_count FROM postings WHERE token_hash=?', (token,)).fetchone()
    if row is None:
        return set()
    raw, count = row
    values = shadow.decode_doc_ids(raw)
    if type(count) is not int or count != len(values) or not values or not set(values) <= valid_numbers:
        raise ValueError('invalid or out-of-scope posting')
    return set(values)


def _verify_snapshot(connection, expected_ids, target_text):
    _schema(connection)
    id_rows = [row[0] for row in connection.execute('SELECT doc_id FROM documents')]
    ids = set(id_rows)
    if ids != expected_ids or len(id_rows) != len(ids):
        raise ValueError('SQLite document IDs differ from complete metadata')
    numbers = {shadow.number(doc_id) for doc_id in ids}
    if {row[0] for row in connection.execute('SELECT rowid FROM content_fts')} != numbers:
        raise ValueError('FTS IDs differ from complete metadata')
    summary = re.sub(r'\s+', ' ', target_text).strip()[:700]
    if connection.execute('SELECT summary FROM documents WHERE doc_id=?', (shadow.TARGET,)).fetchone() != (summary,):
        raise ValueError('current target summary differs')
    if connection.execute('SELECT content FROM content_fts WHERE rowid=0').fetchone() != (shadow.fts_content(target_text),):
        raise ValueError('current target FTS body differs')
    anchors = dict(connection.execute('SELECT token_hash,char_offset FROM snippet_anchors WHERE doc_number=0'))
    if anchors != shadow.snippet_anchors(target_text):
        raise ValueError('current target anchors differ')
    if connection.execute('SELECT COUNT(*) FROM snippet_anchors WHERE doc_number NOT IN '
                          '(SELECT rowid FROM content_fts)').fetchone()[0]:
        raise ValueError('anchors reference unknown documents')
    if connection.execute("SELECT COUNT(*) FROM snippet_anchors WHERE typeof(token_hash)!='blob' OR length(token_hash)!=16 "
                          "OR typeof(doc_number)!='integer' OR typeof(char_offset)!='integer' OR char_offset<0").fetchone()[0]:
        raise ValueError('invalid anchor identity or offset')
    return numbers


def _verify_postings(connection, numbers, target_tokens):
    found = set()
    seen = set()
    for token, raw, count in connection.execute('SELECT token_hash,docs,doc_count FROM postings'):
        if not isinstance(token, bytes) or len(token) != 16 or token in seen:
            raise ValueError('invalid token hash')
        seen.add(token)
        values = shadow.decode_doc_ids(raw)
        if (type(count) is not int or count != len(values) or not values or not set(values) <= numbers
                or (0 in values) != (token in target_tokens)):
            raise ValueError('postings do not match the exact target')
        if 0 in values:
            found.add(token)
    if found != target_tokens:
        raise ValueError('target postings are incomplete')


def _table_counts(connection):
    """Actual candidate counts, not the parent manifest's possibly stale stats."""
    return {name: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for name, table in (('documents', 'documents'), ('fts_rows', 'content_fts'),
                                ('postings', 'postings'), ('snippet_anchors', 'snippet_anchors'))}


def build_candidate(*, space_revision: str, metadata_gz: bytes, pointer_bytes: bytes,
                    activation_bytes: bytes, current_txt: bytes, next_txt: bytes, database_bytes: bytes) -> dict:
    """Return candidate database bytes after validation; never publish or write files."""
    if not hasattr(sqlite3.Connection, 'deserialize') or not hasattr(sqlite3.Connection, 'serialize'):
        raise RuntimeError('Python SQLite serialize/deserialize support is required')
    if not isinstance(database_bytes, bytes) or not database_bytes:
        raise ValueError('complete CW database bytes are required')
    old_digest = hashlib.sha256(database_bytes).hexdigest()
    plan = planner.plan_update(space_revision=space_revision, metadata_gz=metadata_gz, pointer_bytes=pointer_bytes,
                               activation_bytes=activation_bytes, current_txt=current_txt, next_txt=next_txt)
    pointer = strict_json_loads(pointer_bytes)
    database = pointer['databases']['CW']
    if (type(database.get('bytes')) is not int or database['bytes'] != len(database_bytes)
            or database['sha256'] != old_digest):
        raise ValueError('CW database bytes differ from the pinned manifest')
    if (not isinstance(pointer.get('builder'), dict)
            or pointer['builder'].get('sha256') not in APPROVED_BUILDERS
            or pointer.get('tokenizer_version') != TOKENIZER):
        raise ValueError('CW builder or tokenizer is not the reviewed historical identity')
    current = current_txt.decode('utf-8')
    new = next_txt.decode('utf-8')
    metadata = strict_json_loads(gzip.decompress(metadata_gz))
    expected_ids = {record['doc_id'] for record in metadata['records'] if record['source'] == 'CW'}
    old_tokens, new_tokens = shadow.token_hashes(current), shadow.token_hashes(new)
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.deserialize(database_bytes)
        numbers = _verify_snapshot(connection, expected_ids, current)
        # Check all existing postings, not just changed tokens: no extraneous
        # target membership, malformed encoding, or unknown ID may survive.
        _verify_postings(connection, numbers, old_tokens)
        # Force FTS5 to compare its index against its own content table. This is
        # an in-memory check and never changes the supplied bytes.
        connection.execute("INSERT INTO content_fts(content_fts,rank) VALUES('integrity-check',1)")
        connection.commit()
        if current_txt == next_txt:
            return {'candidate_bytes': database_bytes, 'candidate_sha256': hashlib.sha256(database_bytes).hexdigest(),
                    'counts': _table_counts(connection), 'changed': False,
                    'publication_ready': False, 'remote_writes': 0, 'plan': plan}
        with connection:
            connection.execute('UPDATE documents SET summary=? WHERE doc_id=?',
                               (re.sub(r'\s+', ' ', new).strip()[:700], shadow.TARGET))
            connection.execute('DELETE FROM content_fts WHERE rowid=0')
            connection.execute('INSERT INTO content_fts(rowid,content) VALUES(0,?)', (shadow.fts_content(new),))
            connection.execute('DELETE FROM snippet_anchors WHERE doc_number=0')
            connection.executemany('INSERT INTO snippet_anchors VALUES(?,0,?)', shadow.snippet_anchors(new).items())
            # SQLite page layout must not depend on Python's randomized hash
            # seed when reconstructing the same candidate after interruption.
            for token in sorted(old_tokens | new_tokens):
                values = _posting(connection, token, numbers)
                values.discard(0)
                if token in new_tokens:
                    values.add(0)
                if values:
                    connection.execute('INSERT OR REPLACE INTO postings VALUES(?,?,?)',
                                       (token, shadow.encode_doc_ids(values), len(values)))
                else:
                    connection.execute('DELETE FROM postings WHERE token_hash=?', (token,))
            _verify_snapshot(connection, expected_ids, new)
            _verify_postings(connection, numbers, new_tokens)
            connection.execute("INSERT INTO content_fts(content_fts,rank) VALUES('integrity-check',1)")
        candidate = connection.serialize()
        return {'candidate_bytes': candidate, 'candidate_sha256': hashlib.sha256(candidate).hexdigest(),
                'counts': _table_counts(connection), 'changed': True,
                'publication_ready': False, 'remote_writes': 0, 'plan': plan}
    except sqlite3.DatabaseError as error:
        raise ValueError('invalid or incompatible CW SQLite database') from error
    finally:
        connection.close()
