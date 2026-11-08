"""Local-only metadata patcher for one verified CCRD CW:0 text change.

Large gzip JSON inputs are streamed. Only explicitly named CW:0 size/summary
fields may change; all other JSON values are independently compared after the
write. This module does not publish metadata or authorize an index transition.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import ijson

try:
    from . import ccrd_text_features as features
except ImportError:
    import ccrd_text_features as features


MAX_COMPRESSED = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
PATHS = ('data/search_data.json.gz', 'data/folder_browser.json.gz',
         'data/initial/sources/CW.json', 'data/sidebar/sources/CW.json')
TARGETS = {
    PATHS[0]: 'records.item.size',
    PATHS[1]: 'CW..files.item.size',
    PATHS[2]: 'results.item.Size',
    PATHS[3]: 'files.item.size',
}


class LimitedReader:
    def __init__(self, raw, limit):
        self.raw, self.limit, self.count = raw, limit, 0

    def read(self, size=-1):
        remaining = self.limit - self.count
        amount = remaining + 1 if size < 0 else min(size, remaining + 1)
        value = self.raw.read(amount)
        self.count += len(value)
        if self.count > self.limit:
            raise ValueError('expanded CCRD metadata exceeds limit')
        return value


def identity(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_COMPRESSED:
        raise ValueError('CCRD metadata input must be a bounded ordinary file')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def _open(path: Path):
    return gzip.open(path, 'rb') if path.name.endswith('.gz') else path.open('rb')


def _item_prefix(path: str) -> str:
    return TARGETS[path].rsplit('.', 1)[0]


def _selected(path: Path, relative: str) -> dict:
    found = None
    with _open(path) as stream:
        items = ijson.items(LimitedReader(stream, MAX_EXPANDED), _item_prefix(relative), use_float=True)
        for item in items:
            if isinstance(item, dict) and item.get('doc_id') == 'CW:0':
                if found is not None:
                    raise ValueError('metadata has duplicate CW:0 entries: ' + relative)
                found = item
    if found is None:
        raise ValueError('metadata must contain exactly one CW:0 entry: ' + relative)
    return found


def _patch_fields(relative: str, item: dict, old_size: int, new_size: int, old_text: str, new_text: str) -> dict:
    field = 'Size' if relative == PATHS[2] else 'size'
    if type(item.get(field)) is not int or item[field] != old_size:
        raise ValueError('CW:0 metadata size differs: ' + relative)
    updates = {field: new_size}
    if relative == PATHS[2]:
        if item.get('snippet') != features.summarize(old_text):
            raise ValueError('CW:0 initial summary differs')
        updates['snippet'] = features.summarize(new_text)
    return updates


class JsonWriter:
    def __init__(self, output):
        self.output = output
        self.stack = []
        self.after_key = False

    def _value(self):
        if self.after_key:
            self.after_key = False
        elif self.stack:
            frame = self.stack[-1]
            if frame['kind'] != 'array':
                raise ValueError('JSON object value has no key')
            if not frame['first']:
                self.output.write(b',')
            frame['first'] = False

    def event(self, kind, value):
        if kind == 'map_key':
            if not self.stack or self.stack[-1]['kind'] != 'map' or self.after_key:
                raise ValueError('invalid JSON map key')
            frame = self.stack[-1]
            if not frame['first']:
                self.output.write(b',')
            frame['first'] = False
            self.output.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode() + b':')
            self.after_key = True
        elif kind in ('start_map', 'start_array'):
            self._value()
            self.output.write(b'{' if kind == 'start_map' else b'[')
            self.stack.append({'kind': 'map' if kind == 'start_map' else 'array', 'first': True})
        elif kind in ('end_map', 'end_array'):
            expected = 'map' if kind == 'end_map' else 'array'
            if not self.stack or self.stack.pop()['kind'] != expected or self.after_key:
                raise ValueError('invalid JSON container ending')
            self.output.write(b'}' if kind == 'end_map' else b']')
        elif kind in ('string', 'number', 'boolean', 'null'):
            self._value()
            self.output.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())
        else:
            raise ValueError('unsupported JSON event')


def rewrite(source: Path, output: Path, relative: str, old_item: dict, updates: dict) -> None:
    target = _item_prefix(relative)
    replacement = {**old_item, **updates}
    skip = 0
    replaced = 0
    with _open(source) as src, output.open('xb') as raw:
        compressed = gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=9, mtime=0) if relative.endswith('.gz') else raw
        try:
            writer = JsonWriter(compressed)
            for prefix, event, value in ijson.parse(LimitedReader(src, MAX_EXPANDED), use_float=True):
                if not skip and prefix == target and event == 'start_map':
                    # Buffer only one bounded entry, then replace it if its ID matches.
                    from ijson.common import ObjectBuilder
                    builder = ObjectBuilder(); builder.event(event, value); skip = 1
                    continue
                if skip:
                    builder.event(event, value)
                    if event in ('start_map', 'start_array'): skip += 1
                    elif event in ('end_map', 'end_array'): skip -= 1
                    if skip == 0:
                        item = builder.value
                        if isinstance(item, dict) and item.get('doc_id') == 'CW:0':
                            if item != old_item: raise ValueError('CW:0 metadata changed between reads')
                            item = replacement; replaced += 1
                        writer._value()
                        compressed.write(json.dumps(item, ensure_ascii=False, sort_keys=True,
                                                    separators=(',', ':'), allow_nan=False).encode())
                    continue
                writer.event(event, value)
            if writer.stack or writer.after_key or replaced != 1:
                raise ValueError('CW:0 metadata rewrite is incomplete')
        finally:
            if compressed is not raw:
                compressed.close()


def verify(source: Path, output: Path, relative: str, old_item: dict, updates: dict) -> None:
    """Independently compare all JSON events except the explicitly changed fields."""
    import itertools
    target = _item_prefix(relative)
    with _open(source) as left, _open(output) as right:
        first = ijson.parse(LimitedReader(left, MAX_EXPANDED), use_float=True)
        second = ijson.parse(LimitedReader(right, MAX_EXPANDED), use_float=True)
        # Changed records are serialized with sorted keys, which may reorder
        # that one object. Compare those objects separately, all other events exactly.
        def normalize(events):
            skip = 0
            for prefix, event, value in events:
                if not skip and prefix == target and event == 'start_map':
                    from ijson.common import ObjectBuilder
                    builder = ObjectBuilder(); builder.event(event, value); skip = 1
                    continue
                if skip:
                    builder.event(event, value)
                    if event in ('start_map', 'start_array'): skip += 1
                    elif event in ('end_map', 'end_array'): skip -= 1
                    if skip == 0:
                        item = builder.value
                        if isinstance(item, dict) and item.get('doc_id') == 'CW:0':
                            item = {**item, **{key: old_item[key] for key in updates}}
                        yield prefix, 'item', item
                    continue
                yield prefix, event, value
        sentinel = object()
        for a, b in itertools.zip_longest(normalize(first), normalize(second), fillvalue=sentinel):
            if a != b:
                raise ValueError('CCRD metadata non-target values changed')
    if _selected(output, relative) != {**old_item, **updates}:
        raise ValueError('CCRD metadata target update differs')


def _input(root: Path, relative: str) -> Path:
    path = root / relative
    if root.is_symlink() or any((root / '/'.join(relative.split('/')[:index])).is_symlink()
                                for index in range(1, len(relative.split('/')) + 1)):
        raise ValueError('CCRD metadata input path contains a symlink')
    if root.resolve() not in path.resolve().parents:
        raise ValueError('CCRD metadata input path escapes its root')
    return path


def build(root: Path, output: Path, *, expected: dict, old_text: str, new_text: str) -> dict:
    if not isinstance(expected, dict) or set(expected) != set(PATHS):
        raise ValueError('exact CCRD metadata input identities are required')
    old_size, new_size = len(old_text.encode()), len(new_text.encode())
    if not old_text or not new_text or old_text == new_text or max(old_size, new_size) > 32 * 1024 * 1024:
        raise ValueError('CCRD text change is empty, unchanged or oversized')
    if (output.exists() or output.is_symlink() or root.resolve() == output.resolve()
            or root.resolve() in output.resolve().parents or output.resolve() in root.resolve().parents):
        raise ValueError('metadata output must be a new directory outside input root')
    inputs = {}
    for relative in PATHS:
        path = _input(root, relative)
        if identity(path) != expected[relative]:
            raise ValueError('CCRD metadata input identity differs: ' + relative)
        inputs[relative] = (_selected(path, relative), path)
    output.mkdir(parents=False, exist_ok=False, mode=0o700)
    results = {}
    try:
        for relative, (item, path) in inputs.items():
            updates = _patch_fields(relative, item, old_size, new_size, old_text, new_text)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            rewrite(path, destination, relative, item, updates)
            verify(path, destination, relative, item, updates)
            if identity(path) != expected[relative]:
                raise ValueError('CCRD metadata source changed during build')
            results[relative] = identity(destination)
    except BaseException:
        shutil.rmtree(output)
        raise
    return {'version': 1, 'kind': 'ccrd-cw0-local-metadata-candidate', 'files': results,
            'old_bytes': old_size, 'new_bytes': new_size, 'all_other_json_values_verified': True,
            'publication_ready': False, 'remote_writes': 0}
