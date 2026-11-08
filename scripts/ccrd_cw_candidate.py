"""Build a local-only candidate replacing CW:0 in a verified CCRD SQLite index.

The source database, old text and new text are exact bounded inputs. The
candidate is a separate file.  Nothing here uploads, changes a manifest, or
activates a consumer. Candidate readiness is intentionally always false.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_postings as postings
    from . import ccrd_text_features as features
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_postings as postings
    import ccrd_text_features as features


MAX_DB_BYTES = 4 * 1024 * 1024 * 1024
MAX_DOCUMENTS = 2_000_000
MAX_TEXT_BYTES = 32 * 1024 * 1024
DOC_ID = 'CW:0'
DOC_NUMBER = 0


def file_identity(path: Path, maximum: int) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError('input must be an ordinary non-symlink file')
    size = path.stat().st_size
    if not 0 < size <= maximum:
        raise ValueError('input file is empty or exceeds its limit')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return {'bytes': size, 'sha256': digest.hexdigest()}


def _checked(path: Path, identity: dict, maximum: int) -> None:
    if (not isinstance(identity, dict) or set(identity) != {'bytes', 'sha256'}
            or type(identity['bytes']) is not int or file_identity(path, maximum) != identity):
        raise ValueError('input identity does not match its approved bytes')
    lifecycle.sha(identity['sha256'])


def _text(path: Path) -> str:
    return path.read_bytes().decode('utf-8')


def _load_posting(db: sqlite3.Connection, token: bytes, documents: int) -> list[int]:
    row = db.execute('SELECT docs, doc_count FROM postings WHERE token_hash = ?', (token,)).fetchone()
    if row is None:
        return []
    if not isinstance(row[0], bytes) or type(row[1]) is not int or row[1] < 1:
        raise ValueError('invalid CCRD posting row')
    return postings.decode(row[0], expected_count=row[1], maximum=documents)


def _preflight(db: sqlite3.Connection, old_text: str, expected_documents: int) -> tuple[set[bytes], dict[bytes, int]]:
    if type(expected_documents) is not int or not 1 <= expected_documents <= MAX_DOCUMENTS:
        raise ValueError('invalid expected document count')
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    if not {'metadata', 'documents', 'content_fts', 'postings', 'snippet_anchors'} <= tables:
        raise ValueError('CCRD CW index lacks required tables')
    version = db.execute("SELECT value FROM metadata WHERE key='tokenizer_version'").fetchone()
    if version != (lifecycle.TOKENIZER,):
        raise ValueError('CCRD CW index tokenizer differs')
    counts = [db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('documents', 'content_fts')]
    if counts != [expected_documents, expected_documents]:
        raise ValueError('CCRD CW index document counts differ')
    if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise ValueError('CCRD CW SQLite quick check failed')
    target = db.execute('SELECT summary FROM documents WHERE doc_id=?', (DOC_ID,)).fetchall()
    fts = db.execute('SELECT content FROM content_fts WHERE rowid=?', (DOC_NUMBER,)).fetchall()
    if target != [(features.summarize(old_text),)] or fts != [(features.normalized(old_text),)]:
        raise ValueError('CCRD CW:0 stored text differs from the approved old text')
    expected_anchors = features.anchors(old_text)
    cursor = db.execute('SELECT token_hash,char_offset FROM snippet_anchors WHERE doc_number=?', (DOC_NUMBER,))
    actual_anchors = {}
    for token, offset in cursor:
        if token in actual_anchors or len(actual_anchors) >= len(expected_anchors):
            raise ValueError('CCRD CW:0 snippet anchors are duplicated or oversized')
        actual_anchors[token] = offset
    if actual_anchors != expected_anchors:
        raise ValueError('CCRD CW:0 snippet anchors differ from approved old text')
    old_tokens = features.token_hashes(old_text)
    for token in old_tokens:
        if DOC_NUMBER not in _load_posting(db, token, expected_documents):
            raise ValueError('CCRD CW:0 expected posting is absent')
    return old_tokens, expected_anchors


def _update_postings(db: sqlite3.Connection, old_tokens: set[bytes], new_tokens: set[bytes], documents: int) -> None:
    for token in sorted(old_tokens | new_tokens):
        existing = _load_posting(db, token, documents)
        if (DOC_NUMBER in existing) != (token in old_tokens):
            raise ValueError('CCRD CW:0 posting membership differs from approved old text')
        remaining = [number for number in existing if number != DOC_NUMBER]
        updated = ([DOC_NUMBER] + remaining) if token in new_tokens else remaining
        if updated:
            db.execute('INSERT INTO postings(token_hash,docs,doc_count) VALUES(?,?,?) '
                       'ON CONFLICT(token_hash) DO UPDATE SET docs=excluded.docs,doc_count=excluded.doc_count',
                       (token, postings.encode(updated), len(updated)))
        else:
            db.execute('DELETE FROM postings WHERE token_hash=?', (token,))


def audit_unchanged(source: Path, candidate: Path, *, documents: int) -> dict:
    """Compare all non-CW:0 logical rows and posting memberships, never a sample."""
    started = time.monotonic()
    old = sqlite3.connect(f'file:{source.resolve()}?mode=ro&immutable=1', uri=True)
    new = sqlite3.connect(f'file:{candidate.resolve()}?mode=ro&immutable=1', uri=True)
    try:
        row_counts = {}
        for name, query in (
            ('documents', "SELECT doc_id,summary FROM documents WHERE doc_id != 'CW:0' ORDER BY doc_id"),
            ('fts', 'SELECT rowid,content FROM content_fts WHERE rowid != 0 ORDER BY rowid'),
            ('anchors', ('SELECT token_hash,doc_number,char_offset FROM snippet_anchors WHERE doc_number != 0 '
                         'ORDER BY token_hash,doc_number')),
            ('metadata', 'SELECT key,value FROM metadata ORDER BY key'),
        ):
            left, right, count = old.execute(query), new.execute(query), 0
            while True:
                first, second = left.fetchmany(2000), right.fetchmany(2000)
                if first != second:
                    raise ValueError('CCRD non-target rows changed: ' + name)
                count += len(first)
                if not first:
                    break
            row_counts[name] = count
        posting_counts = _audit_postings(old, new, documents)
        if new.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('CCRD candidate SQLite integrity check failed')
        return {'non_target_rows': row_counts, **posting_counts,
                'sqlite_integrity': 'ok', 'seconds': round(time.monotonic() - started, 3)}
    finally:
        old.close()
        new.close()


def _audit_postings(old: sqlite3.Connection, new: sqlite3.Connection, documents: int) -> dict:
    first = old.execute('SELECT token_hash,docs,doc_count FROM postings ORDER BY token_hash')
    second = new.execute('SELECT token_hash,docs,doc_count FROM postings ORDER BY token_hash')
    left, right = first.fetchone(), second.fetchone()
    old_rows = new_rows = changed = 0
    while left is not None or right is not None:
        if right is None or left is not None and left[0] < right[0]:
            old_row, new_row = left, None
            left = first.fetchone()
        elif left is None or right[0] < left[0]:
            old_row, new_row = None, right
            right = second.fetchone()
        else:
            old_row, new_row = left, right
            left, right = first.fetchone(), second.fetchone()
        old_rows += old_row is not None
        new_rows += new_row is not None
        if old_row and new_row and old_row[1:] == new_row[1:]:
            continue
        if ((old_row and (not isinstance(old_row[0], bytes) or len(old_row[0]) != 16
                         or type(old_row[2]) is not int or old_row[2] < 1))
                or (new_row and (not isinstance(new_row[0], bytes) or len(new_row[0]) != 16
                                 or type(new_row[2]) is not int or new_row[2] < 1))):
            raise ValueError('CCRD changed posting row has an invalid identity or count')
        old_ids = postings.decode(old_row[1], expected_count=old_row[2], maximum=documents) if old_row else []
        new_ids = postings.decode(new_row[1], expected_count=new_row[2], maximum=documents) if new_row else []
        if [value for value in old_ids if value != DOC_NUMBER] != [value for value in new_ids if value != DOC_NUMBER]:
            raise ValueError('CCRD non-target posting membership changed')
        changed += 1
    return {'old_posting_rows': old_rows, 'new_posting_rows': new_rows, 'changed_posting_keys_checked': changed}


def _modify(db: sqlite3.Connection, old_text: str, new_text: str, expected_documents: int) -> None:
    old_tokens, _old_anchors = _preflight(db, old_text, expected_documents)
    new_tokens, new_anchors = features.token_hashes(new_text), features.anchors(new_text)
    _update_postings(db, old_tokens, new_tokens, expected_documents)
    updated = db.execute('UPDATE documents SET summary=? WHERE doc_id=?', (features.summarize(new_text), DOC_ID))
    if updated.rowcount != 1:
        raise ValueError('CCRD CW:0 metadata row changed during update')
    db.execute('DELETE FROM content_fts WHERE rowid=?', (DOC_NUMBER,))
    db.execute('INSERT INTO content_fts(rowid,content) VALUES(?,?)', (DOC_NUMBER, features.normalized(new_text)))
    db.execute('DELETE FROM snippet_anchors WHERE doc_number=?', (DOC_NUMBER,))
    db.executemany('INSERT INTO snippet_anchors(token_hash,doc_number,char_offset) VALUES(?,?,?)',
                   ((token, DOC_NUMBER, offset) for token, offset in sorted(new_anchors.items())))


def build(source: Path, old: Path, new: Path, output: Path, *, source_identity: dict, old_identity: dict,
          new_identity: dict, expected_documents: int) -> dict:
    _checked(source, source_identity, MAX_DB_BYTES)
    _checked(old, old_identity, MAX_TEXT_BYTES)
    _checked(new, new_identity, MAX_TEXT_BYTES)
    if old_identity == new_identity:
        raise ValueError('CCRD source text is unchanged')
    old_text, new_text = _text(old), _text(new)
    if output.exists() or output.is_symlink() or output.resolve() in {p.resolve() for p in (source, old, new)}:
        raise ValueError('candidate output must be a new path')
    # A private directory owns the SQLite sidecars and avoids overwriting or
    # deleting a concurrently-created destination after the initial check.
    output.mkdir(parents=False, exist_ok=False, mode=0o700)
    database = output / 'CW.sqlite3'
    try:
        with source.open('rb') as src, database.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        with sqlite3.connect(database) as db, db:
            _modify(db, old_text, new_text, expected_documents)
        if (file_identity(source, MAX_DB_BYTES) != source_identity or file_identity(old, MAX_TEXT_BYTES) != old_identity
                or file_identity(new, MAX_TEXT_BYTES) != new_identity):
            raise ValueError('approved input changed while candidate was built')
        with sqlite3.connect(f'file:{database.resolve()}?mode=ro&immutable=1', uri=True) as check:
            _preflight(check, new_text, expected_documents)
        audit = audit_unchanged(source, database, documents=expected_documents)
        result = file_identity(database, MAX_DB_BYTES)
    except BaseException:
        shutil.rmtree(output)
        raise
    return {'version': 1, 'kind': 'ccrd-cw0-local-candidate', 'source': source_identity, 'old_text': old_identity,
            'new_text': new_identity, 'candidate': {'file': 'CW.sqlite3', **result}, 'documents': expected_documents,
            'non_target_audit': audit, 'publication_ready': False, 'remote_writes': 0,
            'scope': 'local CW:0 candidate only; FTS index internals, other sources and consumer delivery not verified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'old', 'new', 'output', 'identities', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--documents', required=True, type=int)
    args = parser.parse_args(argv)
    if args.report.exists() or args.report.is_symlink() or args.report.resolve() == args.output.resolve():
        raise ValueError('candidate report must be a new separate path')
    if (args.identities.is_symlink() or not args.identities.is_file() or args.identities.stat().st_size > 64 * 1024
            or args.report.resolve() in {args.source.resolve(), args.old.resolve(), args.new.resolve(), args.identities.resolve()}
            or args.output.resolve() == args.identities.resolve()):
        raise ValueError('candidate report or identities path is unsafe')
    values = json.loads(args.identities.read_bytes())
    if not isinstance(values, dict) or set(values) != {'source', 'old', 'new'}:
        raise ValueError('candidate identities are incomplete')
    result = build(args.source, args.old, args.new, args.output, source_identity=values['source'],
                   old_identity=values['old'], new_identity=values['new'], expected_documents=args.documents)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
