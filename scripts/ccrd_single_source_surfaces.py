"""Offline, explicit surface updates for one CCRD TXT; no IO or publication.

Every existing cache path must be supplied by an independent inventory with a
schema and the exact positions of CW:0. Unknown or changed structures fail
closed; the caller must separately prove that its inventory is complete.
"""
from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
import re
from pathlib import PurePosixPath

try:
    from .ccrd_strict_json import loads as strict_json_loads
except ImportError:
    from ccrd_strict_json import loads as strict_json_loads


DOC_ID = 'CW:0'
SCHEMAS = {'initial-v1', 'sidebar-v1', 'folder-browser-v1'}
SIZE_KEYS = {'initial-v1': 'Size', 'sidebar-v1': 'size', 'folder-browser-v1': 'size'}
SUMMARY_CHARS = 700


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_locations(value):
    """Enumerate occurrences without assuming result ordering or page size."""
    locations = []
    stack = [(value, ())]
    while stack:
        item, path = stack.pop()
        if isinstance(item, dict):
            if item.get('doc_id') == DOC_ID:
                locations.append(path)
            stack.extend((child, path + (key,)) for key, child in item.items())
        elif isinstance(item, list):
            stack.extend((child, path + (index,)) for index, child in enumerate(item))
    return sorted(locations, key=lambda path: json.dumps(path, ensure_ascii=False))


def selected(value, path):
    node = value
    for part in path:
        if isinstance(node, dict) and isinstance(part, str) and part in node:
            node = node[part]
        elif isinstance(node, list) and type(part) is int and 0 <= part < len(node):
            node = node[part]
        else:
            raise ValueError('cache occurrence path is invalid')
    return node


def update_payloads(specifications: dict, payloads: dict, current_txt: bytes, next_txt: bytes) -> dict:
    """Update only planned CW:0 size/snippet values in caller-provided bytes."""
    if not isinstance(specifications, dict) or not specifications or not isinstance(payloads, dict):
        raise ValueError('explicit nonempty cache inventory is required')
    if set(specifications) != set(payloads):
        raise ValueError('cache inventory and supplied payloads differ')
    if not isinstance(current_txt, bytes) or not current_txt or not isinstance(next_txt, bytes) or not next_txt:
        raise ValueError('current and proposed TXT must be nonempty bytes')
    try:
        current_text, next_text = current_txt.decode('utf-8'), next_txt.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('target TXT must be valid UTF-8') from error
    summary = lambda text: re.sub(r'\s+', ' ', text).strip()[:SUMMARY_CHARS]
    old_snippet, new_snippet = summary(current_text), summary(next_text)
    outputs, changed, seen_schemas = {}, [], set()
    for name, spec in specifications.items():
        if (not isinstance(name, str)
                or not (name.startswith(('data/initial/', 'data/sidebar/')) or name == 'data/folder_browser.json.gz')
                or '\\' in name or any(part in {'', '.', '..'} for part in name.split('/'))
                or not name.endswith(('.json', '.json.gz')) or PurePosixPath(name).is_absolute()):
            raise ValueError('cache path is outside the explicit allowed roots')
        if (not isinstance(spec, dict) or set(spec) != {'schema', 'sha256', 'positions'}
                or spec['schema'] not in SCHEMAS or not isinstance(spec['positions'], list)):
            raise ValueError('invalid cache inventory entry')
        if (not isinstance(spec['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', spec['sha256'])
                or not isinstance(payloads[name], bytes)):
            raise ValueError('cache inventory requires a complete sha256 and bytes')
        expected_schema = ('initial-v1' if name.startswith('data/initial/') else
                           'sidebar-v1' if name.startswith('data/sidebar/') else 'folder-browser-v1')
        if spec['schema'] != expected_schema:
            raise ValueError('cache schema does not match its root')
        if name.endswith('.gz') != (spec['schema'] == 'folder-browser-v1'):
            raise ValueError('cache compression does not match its schema')
        raw = payloads[name]
        if not isinstance(raw, bytes) or spec['sha256'] != sha(raw):
            raise ValueError('cache bytes differ from the inventoried snapshot')
        try:
            data = strict_json_loads(gzip.decompress(raw) if name.endswith('.gz') else raw)
        except (ValueError, UnicodeDecodeError, EOFError, OSError) as error:
            raise ValueError('cache payload is invalid JSON') from error
        if not isinstance(data, dict):
            raise ValueError('cache payload must be an object')
        positions = spec['positions']
        if any(not isinstance(path, list) or any(not (isinstance(part, str) or type(part) is int) for part in path)
               for path in positions):
            raise ValueError('invalid cache occurrence path')
        actual = target_locations(data)
        expected = sorted((tuple(path) for path in positions), key=lambda path: json.dumps(path, ensure_ascii=False))
        if len(set(expected)) != len(expected) or actual != expected:
            raise ValueError('cache occurrence inventory is incomplete or stale')
        updated = deepcopy(data)
        for path in expected:
            item = selected(updated, path)
            field = SIZE_KEYS[spec['schema']]
            if (not isinstance(item, dict) or item.get('doc_id') != DOC_ID
                    or type(item.get(field)) is not int or item[field] != len(current_txt)):
                raise ValueError('cache target size or identity differs from the current TXT')
            if spec['schema'] == 'initial-v1':
                if item.get('Source') != 'CW' or item.get('snippet') != old_snippet:
                    raise ValueError('initial cache target snippet differs from the current TXT')
                item['snippet'] = new_snippet
            seen_schemas.add(spec['schema'])
            item[field] = len(next_txt)
        if updated == data:
            outputs[name] = raw
        else:
            encoded = json.dumps(updated, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            outputs[name] = gzip.compress(encoded, compresslevel=9, mtime=0) if name.endswith('.gz') else encoded
            changed.append(name)
    if seen_schemas != SCHEMAS:
        raise ValueError('target must be accounted for in initial, sidebar and folder-browser caches')
    return {'payloads': outputs, 'changed_paths': sorted(changed), 'explicit_deletions': [],
            'remote_writes': 0, 'inventory_complete_proven': False, 'publication_ready': False}
