"""Bounded resource caching and temporary page storage for complete book checks."""
from collections import OrderedDict
import hashlib
import json
import tempfile

try:
    from . import pdf_reading_v3 as v3
except ImportError:
    import pdf_reading_v3 as v3


class VerifiedReader:
    def __init__(self, read, *, cache_bytes=8 * 1024 * 1024,
                 max_resource_bytes=128 * 1024 * 1024, max_input_bytes=None):
        self.read = read
        self.cache_limit = cache_bytes
        self.resource_limit = max_resource_bytes
        self.total_limit = max_input_bytes
        self.cache = OrderedDict()
        self.identities = {}
        self.cache_bytes = self.peak_cache_bytes = self.input_bytes = 0

    def __call__(self, ref):
        v3.resource(ref)
        key = (ref['bucket'], ref['path'])
        identity = (ref['sha256'], ref['bytes'])
        previous = self.identities.get(key)
        if previous is not None and previous != identity:
            raise ValueError('conflicting export input identity')
        if ref['bytes'] > self.resource_limit:
            raise ValueError('export resource exceeds input budget; never publish partial text')
        if previous is None and self.total_limit is not None and self.input_bytes + ref['bytes'] > self.total_limit:
            raise ValueError('export exceeds input budget; never publish partial text')
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        raw = self.read(ref)
        if not isinstance(raw, bytes):
            raise ValueError('resource reader must return bytes')
        if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise ValueError('export input checksum mismatch')
        if previous is None:
            self.input_bytes += len(raw)
            self.identities[key] = identity
        if len(raw) <= self.cache_limit:
            while self.cache and self.cache_bytes + len(raw) > self.cache_limit:
                _, removed = self.cache.popitem(last=False)
                self.cache_bytes -= len(removed)
            self.cache[key] = raw
            self.cache_bytes += len(raw)
            self.peak_cache_bytes = max(self.peak_cache_bytes, self.cache_bytes)
        return raw


class TextPageSpool:
    """A page sequence whose decoded geometry occupies memory one page at a time."""
    def __init__(self):
        self.file = tempfile.TemporaryFile(prefix='reader-text-pages-')
        self.offsets = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.file.close()

    def __len__(self):
        return len(self.offsets)

    def append(self, layer):
        raw = json.dumps(layer, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        self.file.seek(0, 2)
        self.offsets.append((self.file.tell(), len(raw)))
        self.file.write(raw)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[number] for number in range(*index.indices(len(self)))]
        start, size = self.offsets[index]
        self.file.seek(start)
        return json.loads(self.file.read(size))

    def __iter__(self):
        for number in range(len(self)):
            yield self[number]
