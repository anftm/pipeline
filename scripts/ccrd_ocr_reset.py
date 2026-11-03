#!/usr/bin/env python3
"""Withdraw the generated OCR library through its existing central publisher."""
from __future__ import annotations

import json
import os
import re
import time

try:
    from . import publish_ccrd_ocr as publisher, ccrd_lifecycle as lifecycle
    from .reader_document_reset import DocumentProducerFence, encode, digest
except ImportError:
    import publish_ccrd_ocr as publisher
    import ccrd_lifecycle as lifecycle
    from reader_document_reset import DocumentProducerFence, encode, digest


def withdraw(options, *, apply=False):
    from huggingface_hub import HfApi, CommitOperationDelete, hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError
    from pathlib import Path
    import httpx

    reset_id = options.get('reset_id')
    if not isinstance(reset_id, str) or not re.fullmatch(r'[a-z0-9-]{8,80}', reset_id):
        raise ValueError('OCR reset requires a stable reset_id')
    if apply:
        publisher.assert_writer()
        DocumentProducerFence().check()
    store = publisher.HubBucketStore()
    api = HfApi(token=os.environ.get('HF_TOKEN'))
    report_path = f'reports/ccrd-ocr-reset/{reset_id}/withdrawal.json'
    try:
        report = json.loads(store.read_bytes(lifecycle.BUCKET, report_path))
    except FileNotFoundError:
        report = {'version': 1, 'kind': 'ccrd-ocr-reset', 'reset_id': reset_id, 'status': 'planned'}
    info = publisher.retry_read(lambda: api.repo_info('vomebook/Search', repo_type='space'))
    variables = publisher.retry_read(lambda: api.get_space_variables('vomebook/Search'))
    if any(getattr(variables.get(name), 'value', '') for name in
           ('CCRD_OCR_RELEASE_PATH', 'CCRD_OCR_RELEASE_SHA256')):
        raise ValueError('Space has an independent OCR environment override')
    def download(path):
        return Path(publisher.retry_read(lambda: hf_hub_download('vomebook/Search', path,
            revision=info.sha, repo_type='space', token=os.environ.get('HF_TOKEN')))).read_bytes()
    # Freeze the independent base activation. Removing OCR must not touch the
    # CCRD/CW/newspaper generation or any of its source archives.
    base = download('data/index-release.json')
    report.setdefault('base_activation_sha256', digest(base))
    if report['base_activation_sha256'] != digest(base):
        raise ValueError('independent base activation changed during OCR reset')
    try:
        activation = download('data/ocr-release.json')
    except EntryNotFoundError:
        activation = None
    if activation:
        report.setdefault('previous_activation', json.loads(activation))
    if not apply:
        return report
    store.put_bytes(lifecycle.BUCKET, report_path, encode(report))
    if activation:
        commit = api.create_commit('vomebook/Search', repo_type='space', parent_commit=info.sha,
            commit_message='Withdraw generated OCR library for document pipeline rebuild',
            operations=[CommitOperationDelete(path_in_repo='data/ocr-release.json')])
        report.update(status='waiting-for-consumer', space_revision=commit.oid)
        store.put_bytes(lifecycle.BUCKET, report_path, encode(report))
    deadline = time.monotonic() + 2400
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        while True:
            try:
                response = client.get('https://vomebook-search.hf.space/api/index-status',
                                      headers={'Cache-Control': 'no-cache'})
                response.raise_for_status()
                status = response.json()
                if (status.get('ready') is True and status.get('ocr_release') is None
                        and {'CCRD', 'CW', 'RMRB', 'PLA'} <= set(status.get('loaded_sources', []))):
                    report['consumer'] = status
                    break
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('OCR deactivation has not reached the live consumer')
            time.sleep(20)
    # Control roots are retired before their data. Audit keeps the previous
    # activation and byte identities; old page/text content is not duplicated.
    paths = store.list_files(lifecycle.BUCKET, ('control/intents/', 'control/retained/'))
    audit = {entry['path']: entry for entry in report.get('retired_control', [])}
    for path in sorted(paths):
        raw = store.read_bytes(lifecycle.BUCKET, path)
        value = json.loads(raw)
        lifecycle.references(value, path)
        audit.setdefault(path, {'path': path, 'sha256': digest(raw), 'bytes': len(raw),
                               'generation': value.get('generation'), 'status': value.get('status')})
        if value.get('status') == 'retired' and not value.get('resources'):
            continue
        retired = {k: value[k] for k in ('version', 'kind', 'generation', 'expected_parent') if k in value}
        retired.update(status='retired', resources=[], retirement_reason='explicit-document-reset', reset_id=reset_id)
        store.put_bytes(lifecycle.BUCKET, path, encode(retired))
    try:
        old = store.read_bytes(lifecycle.BUCKET, publisher.CURRENT)
    except FileNotFoundError:
        old = None
    if old:
        report['previous_pointer'] = json.loads(old)
        store.put_bytes(lifecycle.BUCKET, report_path, encode(report))
        store.delete(lifecycle.BUCKET, [publisher.CURRENT])
    for path in ('control/ocr-consumer.json', 'control/ocr-ids.json'):
        try:
            value = json.loads(store.read_bytes(lifecycle.BUCKET, path))
        except FileNotFoundError:
            continue
        lifecycle.references(value, path)
        value.update(resources=[], status='withdrawn', reset_id=reset_id)
        store.put_bytes(lifecycle.BUCKET, path, encode(value))
    report.update(status='withdrawn', retired_control=list(audit.values()), applied=True)
    store.put_bytes(lifecycle.BUCKET, report_path, encode(report))
    return report
