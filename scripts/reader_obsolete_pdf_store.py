"""One-attempt conditional reference edits for the reviewed obsolete PDF set."""
import hashlib
import json
import re

from . import reader_obsolete_pdf_apply as operation
from .reader_page_history_store import ConditionalPageHistoryStore


class ObsoleteReferenceStore(ConditionalPageHistoryStore):
    def __init__(self, primary, mutations, s3=None):
        super().__init__(primary, s3)
        ordered = operation._ordered(mutations)
        self.expected = {(m['bucket'], m['path']): m for m in ordered}

    def _require_root(self, bucket, path):
        if (bucket, path) not in self.expected:
            raise ValueError('obsolete reference write outside exact reviewed roots')

    def put_if_match(self, bucket, path, raw, token):
        self._require_root(bucket, path)
        if raw != self.expected[bucket, path]['after'] or token is None:
            raise ValueError('obsolete reference write differs from reviewed after bytes')
        return super().put_if_match(bucket, path, raw, token)

    @staticmethod
    def _immutable_type(bucket, path, raw):
        match = re.fullmatch(re.escape(operation.INTENTS) + r'([a-f0-9]{64})\.json', path)
        if bucket != operation.ASSETS or not match or not isinstance(raw, bytes):
            raise ValueError('obsolete reference audit outside exact namespace')
        value = json.loads(raw)
        if (value.get('kind') != 'reader-obsolete-pdf-retirement-intent'
                or operation.preparation.p.encode(value) != raw
                or hashlib.sha256(raw).hexdigest() != match[1]):
            raise ValueError('obsolete reference audit identity mismatch')
        operation._ordered([operation._unpacked(m) for m in value['mutations']])
        return 'application/json'
