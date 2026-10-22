#!/usr/bin/env python3
"""Commit only the accepted consumer pin via the existing bot state-commit path."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone

try:
    from . import auto_ccrd_ocr as auto, ccrd_ocr_release as contract, publish_ccrd_ocr as publisher
except ImportError:
    import auto_ccrd_ocr as auto
    import ccrd_ocr_release as contract
    import publish_ccrd_ocr as publisher


def accepted_pin(publication, acceptance):
    pin = auto.configuration(publication.get('accepted_consumer', {}))
    if (publication.get('applied') is not True
            or publication.get('space_revision') != pin['consumer_revision']
            or acceptance.get('kind') != 'ccrd-newspaper-live-acceptance'
            or acceptance.get('generation') != publication.get('generation')
            or acceptance.get('base_metadata_sha256') != pin['base_metadata_sha256']
            or acceptance.get('OCR', {}).get('generation') != publication.get('OCR_generation')
            or acceptance.get('OCR', {}).get('complete_id_set_verified') is not True
            or acceptance.get('mixed_zip_verified') is not True
            or acceptance.get('first_last_API_pages_checked') is not True):
        raise ValueError('consumer pin requires successful exact four-source production acceptance')
    return pin


def finish_journal(store, publication, pin):
    path = 'control/newspaper-rollout.json'
    journal, _raw = publisher.optional_json(store, path)
    if (not journal or journal.get('generation') != publication['generation']
            or journal.get('space_revision') != pin['consumer_revision']
            or journal.get('base_metadata_sha256') != pin['base_metadata_sha256']):
        raise ValueError('accepted consumer does not match the protected rollout journal')
    journal.update(status='committed', updated_at=datetime.now(timezone.utc).isoformat(),
                   resources=[ref for ref in journal['resources']
                              if not ref['path'].startswith(('releases/ocr-', 'indexes/ocr/'))])
    # Ordinary OCR publication/retirement resumes after this point. Historical
    # OCR activation identities are audit fields, not permanent GC roots.
    consumer_path = 'control/ocr-consumer.json'
    consumer, _ = publisher.optional_json(store, consumer_path)
    if not consumer or consumer.get('consumer_revision') != pin['consumer_revision']:
        raise ValueError('bucket consumer control differs from the accepted Space revision')
    consumer['resources'] = [ref for ref in consumer['resources']
                             if not ref['path'].startswith(('releases/ocr-', 'indexes/ocr/'))]
    publisher.put_checked(store, consumer_path, contract.canonical(consumer))
    publisher.put_checked(store, path, contract.canonical(journal))


def publish_pin(pin, path=Path('ccrd-ocr-consumer.json')):
    """Rebase only this bot's one-file commit over unrelated upstream changes."""
    def run(args, **kwargs):
        return subprocess.run(['git', *args], check=True, **kwargs)
    def remote_pin():
        run(['fetch', 'origin', 'main'])
        return json.loads(subprocess.check_output(['git', 'show', 'origin/main:' + path.as_posix()], text=True))
    if subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip():
        raise RuntimeError('consumer state commit requires a clean checkout')
    original = json.loads(path.read_text())
    if remote_pin() not in [original, pin]:
        raise ValueError('another writer changed the approved consumer; refusing to replace it')
    if original != pin:
        path.write_bytes(contract.canonical(pin))
        run(['config', 'user.name', 'github-actions[bot]'])
        run(['config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'])
        run(['add', '--', str(path)])
        run(['commit', '-m', 'Pin production-accepted newspaper/OCR consumer [skip ci]'])
        for attempt in range(3):
            upstream = remote_pin()
            if upstream == pin:
                break
            if upstream != original:
                raise ValueError('concurrent approved consumer change blocks the state commit')
            run(['rebase', 'origin/main'])
            pushed = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], check=False)
            if pushed.returncode == 0:
                break
        else:
            raise RuntimeError('could not publish the accepted consumer pin after three safe retries')
    if remote_pin() != pin:
        raise ValueError('accepted consumer pin remote readback differs after push')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication', type=Path, required=True)
    parser.add_argument('--acceptance', type=Path, required=True)
    args = parser.parse_args()
    publisher.assert_writer()
    publication = json.loads(args.publication.read_text())
    pin = accepted_pin(publication, json.loads(args.acceptance.read_text()))
    publish_pin(pin)
    finish_journal(publisher.HubBucketStore(), publication, pin)


if __name__ == '__main__':
    main()
