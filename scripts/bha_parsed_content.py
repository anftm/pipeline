"""Inspect exact parsed Git trees using BHA's publication/article layout.

Counts and explicit ID/tag changes are candidate evidence, not consumer receipts
or permission to delete. Every candidate JSON object is parsed before publishing.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
import subprocess

try:
    from . import search_snapshot
except ImportError:
    import search_snapshot

MAX_BLOB = 32 * 1024 * 1024
MAX_FILES = 2_000_000
SHA = re.compile(r'[0-9a-f]{40}\Z')


def tree_files(repo, revision, env):
    if not SHA.fullmatch(revision):
        raise ValueError('parsed inventory requires exact Git object identity')
    raw = subprocess.check_output(['git','ls-tree','-rz','--full-tree',revision],cwd=repo,env=env)
    entries = raw.split(b'\0')
    if len(entries) > MAX_FILES + 1:
        raise ValueError('parsed tree inventory exceeds bound')
    files = {}
    for entry in entries:
        if not entry: continue
        header, name = entry.split(b'\t',1)
        mode, kind, digest = header.decode('ascii').split()
        path = name.decode('utf-8',errors='strict')
        if (mode not in {'100644','100755'} or kind != 'blob' or not SHA.fullmatch(digest)
                or path.startswith('/') or '\\' in path or any(p in {'','.','..'} for p in path.split('/'))
                or any(ord(c)<32 for c in path) or path in files):
            raise ValueError('unsupported parsed tree entry')
        files[path] = digest
    return files


def article_ids(files, archive):
    metadata = {}
    for path in files:
        parts = PurePosixPath(path).parts
        if len(parts)==3 and parts[-1].endswith('.metadata'):
            directory = '/'.join(parts[:-1])
            if directory in metadata:
                raise ValueError('multiple publication metadata files in one directory')
            metadata[directory] = PurePosixPath(path).stem
    ids = {}
    for path in files:
        parts = PurePosixPath(path).parts
        if len(parts)!=4 or not parts[-1].endswith('.json'): continue
        publication = metadata.get('/'.join(parts[:2]))
        if publication is None:
            raise ValueError('article lacks publication metadata')
        stem = PurePosixPath(path).stem
        identity = f'{archive}:{len(stem)}:{stem}:{publication}'
        if identity in ids:
            raise ValueError('duplicate parsed document identity')
        ids[identity] = path
    indexed_paths = set(ids.values())
    for path in files:
        if path.endswith('.tags') and str(PurePosixPath(path).with_suffix('.json')) not in indexed_paths:
            raise ValueError('tag sidecar has no indexed article')
    return ids


def validate_json_blobs(repo, files, ids, env):
    selected = [(p, digest) for p,digest in files.items()
                if p.endswith(('.json','.metadata','.tags'))]
    process = subprocess.Popen(['git','cat-file','--batch'],cwd=repo,env=env,
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        for path,digest in selected:
            process.stdin.write((digest+'\n').encode());process.stdin.flush()
            fields = process.stdout.readline().decode('ascii').strip().split()
            if len(fields)!=3 or fields[0]!=digest or fields[1]!='blob':
                raise ValueError('parsed blob read identity differs')
            size=int(fields[2])
            if not 0 <= size <= MAX_BLOB: raise ValueError('parsed JSON blob exceeds bound')
            raw=process.stdout.read(size)
            if len(raw)!=size or process.stdout.read(1)!=b'\n':raise ValueError('short parsed blob read')
            if hashlib.sha1(f'blob {size}\0'.encode()+raw).hexdigest()!=digest:
                raise ValueError('parsed blob hash mismatch')
            value=json.loads(raw)
            if path.endswith('.tags'):
                if not isinstance(value,list) or any(not isinstance(tag,dict) for tag in value):
                    raise ValueError('tag sidecar must contain objects')
            elif not isinstance(value,dict):
                raise ValueError('parsed metadata/article must be an object')
        process.stdin.close()
        if process.wait(timeout=10)!=0:raise ValueError('parsed blob reader failed')
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=10)
        if process.stdin and not process.stdin.closed:process.stdin.close()
        process.stdout.close()


def inspect(repo, before, after, archive, env):
    if type(archive) is not int or not 0<=archive<32:raise ValueError('invalid archive')
    old = tree_files(repo,before,env); new = tree_files(repo,after,env)
    old_ids = article_ids(old,archive); new_ids = article_ids(new,archive)
    validate_json_blobs(repo,new,new_ids,env)
    changed_tags = sorted(p for p in old.keys() | new.keys()
                          if p.endswith('.tags') and old.get(p)!=new.get(p))
    def digest(value):return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()
    removed=sorted(old_ids.keys()-new_ids.keys());added=sorted(new_ids.keys()-old_ids.keys())
    return {'kind':'bha-parsed-content-review','archive':archive,'before_tree':before,'tree':after,
            'before_documents':len(old_ids),'documents':len(new_ids),
            'before_document_ids_sha256':digest(sorted(old_ids)),
            'document_ids_sha256':digest(sorted(new_ids)),
            'added_document_ids':added,'removed_document_ids':removed,
            'changed_tag_paths':changed_tags,'json_blobs_verified':True,
            'removed_documents':len(removed),'added_documents':len(added),
            'missing_tag_sidecars':sum(str(PurePosixPath(p).with_suffix('.tags')) not in new for p in new_ids.values()),
            'consumer_verified':False}
