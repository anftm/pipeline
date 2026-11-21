"""Durable, single-attempt S3 journal for the legacy image publication stage."""
from __future__ import annotations

import json
from dataclasses import asdict

from . import reader_image_history as history, reader_image_history_publish as publication
from .reader_image_history_store import S3ClaimStore
from .reader_stream_checkpoint_cas import strong_etag

PREFIX = 'reader-index/gc/image-history-legacy/v1/'
KIND = 'reader-image-legacy-journal'
MAX_RECORD = 8192


def _path(key):
    history._hex(key)
    return PREFIX + key + '.json'


def _encode(key, record):
    if type(record) is not publication.Record:
        raise ValueError('invalid legacy image publication record')
    history._hex(record.seal)
    expected = {'catalog-permitted': (False, False), 'sidecar-permitted': (True, False),
                'legacy-observed': (True, True)}
    if expected.get(record.state) != tuple(t is not None for t in (record.catalog_etag, record.sidecar_etag)):
        raise ValueError('invalid legacy image publication phase')
    for tag in (record.catalog_etag, record.sidecar_etag):
        if tag is not None:
            strong_etag(tag)
    raw = history._encode(dict(version=1, kind=KIND, parent_sha256=key, record=asdict(record)))
    if len(raw) > MAX_RECORD:
        raise ValueError('oversized legacy image publication record')
    return raw


def _decode(key, raw):
    if len(raw) > MAX_RECORD:
        raise ValueError('oversized legacy image publication record')
    try:
        record = publication.Record(**json.loads(raw)['record'])
        if _encode(key, record) != raw:
            raise ValueError('noncanonical or wrong parent record')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid legacy image publication record') from error
    return record


class LegacyJournal:
    def __init__(self, client):
        if client is None:
            raise ValueError('explicit single-attempt S3 client required')
        self.transport = S3ClaimStore(client)

    def _read(self, key):
        raw, etag = self.transport._get(_path(key), maximum=MAX_RECORD)
        return (None, None) if raw is None else (_decode(key, raw), etag)

    def read(self, key):
        return self._read(key)[0]

    def claim(self, key, record):
        data = _encode(key, record)
        if record.state != 'catalog-permitted':
            raise ValueError('invalid initial legacy image publication stage')
        return self.transport._put(_path(key), data, maximum=MAX_RECORD, IfNoneMatch='*')

    def advance(self, key, expected, record):
        _encode(key, expected)
        data = _encode(key, record)
        if (record.seal != expected.seal
                or (expected.state, record.state) not in {('catalog-permitted', 'sidecar-permitted'),
                                                          ('sidecar-permitted', 'legacy-observed')}
                or expected.catalog_etag is not None and record.catalog_etag != expected.catalog_etag):
            raise ValueError('invalid legacy image publication advance')
        actual, etag = self._read(key)
        if actual != expected:
            return False
        return self.transport._put(_path(key), data, maximum=MAX_RECORD, IfMatch=etag)
