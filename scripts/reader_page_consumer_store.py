"""Conditional writes confined to the Reader consumer-root refresh."""
import hashlib
import json
import re

try:
    from . import publish_reader_v3 as publication
    from .reader_page_history_store import ConditionalPageHistoryStore
except ImportError:
    import publish_reader_v3 as publication
    from reader_page_history_store import ConditionalPageHistoryStore

CONSUMERS = 'reader-index/reader_gc_consumers.json'
INTENTS = 'reader-index/gc/consumer-refresh/'


class ConditionalConsumerStore(ConditionalPageHistoryStore):
    def digest(self, bucket, path):
        return self.primary.digest(bucket, path)

    def read_graph_json(self, bucket, path, parser=None):
        return self.primary.read_graph_json(bucket, path, parser=parser)

    @staticmethod
    def _require_root(bucket, path):
        if bucket != publication.ASSETS or path != CONSUMERS:
            raise ValueError('consumer refresh write is outside the allowed root')

    @staticmethod
    def _immutable_type(bucket, path, raw):
        match = re.fullmatch(re.escape(INTENTS) + r'([0-9a-f]{64})\.json', path)
        if bucket != publication.ASSETS or not match or not isinstance(raw, bytes):
            raise ValueError('consumer refresh audit is outside the allowlist')
        value = json.loads(raw)
        if (not isinstance(value, dict) or publication.encode(value) != raw
                or hashlib.sha256(raw).hexdigest() != match[1]):
            raise ValueError('consumer refresh audit identity differs')
        return 'application/json'
