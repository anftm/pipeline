"""Verify and compare bounded local Git trees for one BHA parsed candidate.

This module reads only the named Git objects. It does not execute their content,
checkout worktrees, contact a remote, create commits, publish or reindex. The
result is not authorization, a consumer receipt, or an external-writer fence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import subprocess
from pathlib import Path

try:
    from . import bha_consumer_sample as consumer
    from . import search_snapshot
except ImportError:
    import bha_consumer_sample as consumer
    import search_snapshot


REV = re.compile(r'[0-9a-f]{40}\Z')
PART = re.compile(r'[A-Za-z0-9._-]+\Z')
MAX_FILES = 200_000
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_FILE = 12 * 1024 * 1024
MAX_TREE_LIST = 64 * 1024 * 1024
MAX_DIFF_SAMPLES = 128
MAX_GIT_OUTPUT = MAX_TOTAL + MAX_TREE_LIST
MAX_DIFF_OUTPUT = 64 * 1024 * 1024
TIMEOUT = 180


def _revision(value: str) -> str:
    if not isinstance(value, str) or not REV.fullmatch(value):
        raise ValueError('BHA candidate must name an exact Git tree ID')
    return value


def _limit_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_GIT_OUTPUT, MAX_GIT_OUTPUT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _env(repo: Path) -> dict[str, str]:
    return {'PATH': os.defpath, 'HOME': '/dev/null', 'LANG': 'C', 'LC_ALL': 'C',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
            'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'protocol.allow', 'GIT_CONFIG_VALUE_0': 'never',
            'GIT_CONFIG_KEY_1': 'core.hooksPath', 'GIT_CONFIG_VALUE_1': '/dev/null',
            'GIT_OBJECT_DIRECTORY': str(repo / 'objects'), 'GIT_ALTERNATE_OBJECT_DIRECTORIES': ''}


def _git(repo: Path, *args: str, input_bytes: bytes | None = None, limit: int) -> bytes:
    env = _env(repo)
    import tempfile
    with tempfile.TemporaryFile() as output:
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '--git-dir', str(repo), *args],
                       input=input_bytes, stdout=output, stderr=subprocess.PIPE, check=True, timeout=TIMEOUT,
                       env=env, preexec_fn=_limit_output)
        if output.tell() > limit:
            raise ValueError('BHA Git response exceeds inspection limit')
        output.seek(0)
        return output.read(limit + 1)


def _read_exact(stream, length: int) -> bytes:
    parts = bytearray()
    while len(parts) < length:
        chunk = stream.read(length - len(parts))
        if not chunk:
            raise ValueError('BHA Git object response is truncated')
        parts.extend(chunk)
    return bytes(parts)


def _path(value: str, archive: int) -> tuple[str, str, str]:
    if not isinstance(value, str) or '\\' in value or any(ord(ch) < 32 for ch in value):
        raise ValueError('invalid BHA parsed path')
    parts = value.split('/')
    if len(parts) not in (3, 4) or any(not PART.fullmatch(x) or x in {'.', '..'} for x in parts):
        raise ValueError('invalid BHA parsed path')
    if parts[0] != parts[1][:3]:
        raise ValueError('BHA publication path prefix differs')
    if len(parts) == 3:
        if parts[2] != parts[1] + '.metadata':
            raise ValueError('invalid BHA metadata path')
        return 'metadata', parts[1], ''
    name, dot, extension = parts[3].rpartition('.')
    if not dot or extension not in {'json', 'tags'} or not consumer.ARTICLE.fullmatch(name) or parts[2] != name[:3]:
        raise ValueError('invalid BHA article or tags path')
    return extension, parts[1], name


def _inventory(repo: Path, tree: str, archive: int) -> dict[str, dict]:
    tree = _revision(tree)
    listed = _git(repo, 'ls-tree', '-r', '-z', '--full-tree', tree, limit=MAX_TREE_LIST)
    rows, total = {}, 0
    for entry in listed.split(b'\0'):
        if not entry:
            continue
        try:
            header, name = entry.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split(' ')
            path = name.decode('utf-8')
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError('invalid BHA Git tree entry') from error
        if mode != '100644' or kind != 'blob' or not REV.fullmatch(oid) or path in rows:
            raise ValueError('unsupported or duplicate BHA parsed tree entry')
        role, publication, article = _path(path, archive)
        rows[path] = {'oid': oid, 'role': role, 'publication': publication, 'article': article}
        if len(rows) > MAX_FILES:
            raise ValueError('BHA parsed tree exceeds file count limit')
    if not rows:
        raise ValueError('BHA parsed tree is empty')
    ids = sorted({row['oid'] for row in rows.values()})
    request = ''.join(oid + '\n' for oid in ids).encode('ascii')
    raw = _git(repo, 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
               input_bytes=request, limit=MAX_TREE_LIST)
    lines = raw.decode('ascii').splitlines()
    if len(lines) != len(ids):
        raise ValueError('incomplete BHA Git object size inventory')
    sizes = {}
    for expected, line in zip(ids, lines):
        parts = line.split(' ')
        if len(parts) != 3 or parts[0] != expected or parts[1] != 'blob' or not parts[2].isdigit():
            raise ValueError('missing or invalid BHA Git blob')
        size = int(parts[2])
        if not 1 <= size <= MAX_FILE:
            raise ValueError('BHA parsed blob exceeds file size bounds')
        sizes[expected] = size
    for row in rows.values():
        row['bytes'] = sizes[row['oid']]
        total += row['bytes']
    if total > MAX_TOTAL:
        raise ValueError('BHA parsed tree exceeds total byte limit')
    return rows


def _json_object(raw: bytes, role: str):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('BHA JSON has duplicate object keys')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('invalid JSON constant')))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('invalid BHA parsed JSON') from error
    if role == 'metadata' and not isinstance(value, dict):
        raise ValueError('BHA publication metadata is not an object')
    if role == 'json' and (not isinstance(value, dict) or 'tags' in value):
        raise ValueError('BHA parsed article is not a valid article object')
    if role == 'tags' and not isinstance(value, list):
        raise ValueError('BHA tags are not an array')
    return value


def _read_blobs(repo: Path, rows: dict[str, dict]) -> dict[str, str]:
    expected = {row['oid']: row['bytes'] for row in rows.values()}
    ids = sorted(expected)
    env = _env(repo)
    # A temporary input file avoids a stdin/stdout pipe deadlock for large trees.
    import tempfile
    with tempfile.TemporaryFile() as requests, tempfile.TemporaryFile() as results:
        requests.write(''.join(oid + '\n' for oid in ids).encode('ascii')); requests.seek(0)
        with subprocess.Popen(['git', '-c', 'core.hooksPath=/dev/null', '--git-dir', str(repo), 'cat-file', '--batch'],
                              stdin=requests, stdout=results, stderr=subprocess.PIPE, env=env,
                              preexec_fn=_limit_output) as proc:
            try:
                _stdout, stderr = proc.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate()
                raise ValueError('BHA Git blob read exceeded timeout') from None
            if proc.returncode:
                raise ValueError('BHA Git blob read failed: ' + stderr.decode('utf-8', 'replace')[:300])
        if results.tell() > MAX_TOTAL + MAX_TREE_LIST:
            raise ValueError('BHA Git blob response exceeds total bound')
        results.seek(0); digests = {}
        roles = {}
        for row in rows.values():
            roles.setdefault(row['oid'], set()).add(row['role'])
        for oid in ids:
            header = results.readline(160)
            if header != f"{oid} blob {expected[oid]}\n".encode('ascii'):
                raise ValueError('BHA Git blob response header differs')
            raw = _read_exact(results, expected[oid])
            if _read_exact(results, 1) != b'\n':
                raise ValueError('BHA Git blob separator differs')
            if hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != oid:
                raise ValueError('BHA Git blob content differs from its object ID')
            for role in roles[oid]:
                _json_object(raw, role)
            digests[oid] = hashlib.sha256(raw).hexdigest()
        if results.read(1):
            raise ValueError('unexpected trailing BHA Git blob bytes')
    return digests


def inspect(repo: Path, archive: int, tree: str) -> dict:
    if type(archive) is not int or not 0 <= archive <= 31:
        raise ValueError('one BHA archive number from 0 to 31 is required')
    if (repo.is_symlink() or not repo.is_dir() or (repo / 'objects').is_symlink()
            or not (repo / 'objects').is_dir() or (repo / 'objects/info/alternates').exists()
            or (repo / 'objects/info/alternates').is_symlink()):
        raise ValueError('BHA candidate Git directory is missing, symlinked, or uses object alternates')
    tree = _revision(tree)
    rows = _inventory(repo, tree, archive)
    blobs = _read_blobs(repo, rows)
    publications, identities, by_doc = {}, set(), {}
    for path, row in rows.items():
        role, pub, article = row['role'], row['publication'], row['article']
        if role == 'metadata':
            if pub in publications:
                raise ValueError('duplicate BHA publication ID')
            publications[pub] = path
        else:
            ident = (pub, article, role)
            if ident in identities:
                raise ValueError('duplicate BHA article ID')
            identities.add(ident)
            by_doc.setdefault((pub, article), set()).add(role)
        row['sha256'] = blobs[row['oid']]
    if not publications or not by_doc:
        raise ValueError('BHA parsed tree has no publications or articles')
    if any(pub not in publications or roles != {'json', 'tags'} for (pub, _article), roles in by_doc.items()):
        raise ValueError('BHA parsed tree has orphan articles or missing tags')
    canonical = [{'path': path, 'git_oid': row['oid'], 'bytes': row['bytes'], 'sha256': row['sha256']}
                 for path, row in sorted(rows.items())]
    import tempfile
    with tempfile.TemporaryFile() as listing:
        for item in canonical:
            listing.write(search_snapshot.canonical(item))
            if listing.tell() > MAX_DIFF_OUTPUT:
                raise ValueError('BHA candidate inventory exceeds output limit')
        listing.seek(0)
        digest = hashlib.sha256()
        while chunk := listing.read(1024 * 1024):
            digest.update(chunk)
    return {'tree': tree, 'archive': archive, 'publications': len(publications), 'articles': len(by_doc),
            'files': len(rows), 'bytes': sum(row['bytes'] for row in rows.values()),
            'content_inventory_sha256': digest.hexdigest(), '_files': rows}


def _comparison(repo: Path, *, archive: int, parent_tree: str, candidate_tree: str) -> tuple[dict, list[dict]]:
    parent = inspect(repo, archive, parent_tree)
    candidate = inspect(repo, archive, candidate_tree)
    old, new = parent.pop('_files'), candidate.pop('_files')
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    changed = sorted(path for path in old.keys() & new.keys() if old[path]['sha256'] != new[path]['sha256'])
    differences = [{'kind': kind, 'path': path, 'before_sha256': old[path]['sha256'] if path in old else None,
                    'after_sha256': new[path]['sha256'] if path in new else None}
                   for kind, paths in (('added', added), ('removed', removed), ('changed', changed)) for path in paths]
    return ({'version': 1, 'kind': 'bha-parsed-candidate-readonly-comparison', 'archive': archive,
             'parent': parent, 'candidate': candidate, 'identical': not differences,
             'added': len(added), 'removed': len(removed), 'changed': len(changed),
             'sample_paths': {'added': added[:MAX_DIFF_SAMPLES], 'removed': removed[:MAX_DIFF_SAMPLES],
                              'changed': changed[:MAX_DIFF_SAMPLES]},
             'sample_limit': MAX_DIFF_SAMPLES, 'remote_writes': 0, 'consumer_verified': False,
             'publication_ready': False, 'external_writers_verified': False,
             'scope': 'complete bounded local Git tree bytes and JSON shape; no semantic equivalence or deployment'},
            differences)


def _diff_bytes(differences: list[dict]) -> bytes:
    import io
    output = io.BytesIO()
    for row in differences:
        output.write(search_snapshot.canonical(row))
        if output.tell() > MAX_DIFF_OUTPUT:
            raise ValueError('BHA candidate diff exceeds output limit')
    return output.getvalue()


def compare(repo: Path, *, archive: int, parent_tree: str, candidate_tree: str) -> dict:
    report, differences = _comparison(repo, archive=archive, parent_tree=parent_tree, candidate_tree=candidate_tree)
    raw = _diff_bytes(differences)
    return {**report, 'diff_sha256': hashlib.sha256(raw).hexdigest(), 'diff_bytes': len(raw)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git-dir', required=True, type=Path)
    parser.add_argument('--archive', required=True, type=int)
    parser.add_argument('--parent-tree', required=True)
    parser.add_argument('--candidate-tree', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--diff-output', required=True, type=Path)
    args = parser.parse_args(argv)
    outputs = (args.output, args.diff_output)
    if (args.output.resolve() == args.diff_output.resolve() or any(path.exists() or path.is_symlink()
            or path.resolve().is_relative_to(args.git_dir.resolve()) for path in outputs)):
        raise ValueError('BHA comparison outputs must be new distinct paths outside the Git directory')
    report, differences = _comparison(args.git_dir, archive=args.archive, parent_tree=args.parent_tree,
                                      candidate_tree=args.candidate_tree)
    raw = _diff_bytes(differences)
    result = {**report, 'diff_sha256': hashlib.sha256(raw).hexdigest(), 'diff_bytes': len(raw)}
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    with args.diff_output.open('xb') as output:
        output.write(raw)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
