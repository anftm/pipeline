#!/usr/bin/env python3
"""Read-only BHA source mirror coverage at two pinned repository revisions."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
from urllib.parse import urlparse

from huggingface_hub import HfApi

try:
    from . import audit_bha_parsed as audit
except ImportError:
    import audit_bha_parsed as audit

DATASET = 'vomebook/BHA-Source-Files'
SOURCE_OWNER = 'banned-historical-archives'
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_METADATA_FILES = 10000


def urls_in_snapshot(path: Path) -> set[str]:
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError('parsed snapshot exceeds inspection limit')
    urls, count, names = set(), 0, set()
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith('.metadata'):
                continue
            relative = audit.relative_member_path(member.name)
            if relative is None or relative.as_posix() in names:
                raise ValueError('invalid or duplicate metadata path')
            names.add(relative.as_posix()); count += 1
            if count > MAX_METADATA_FILES or member.size > MAX_METADATA_BYTES:
                raise ValueError('parsed metadata exceeds inspection limits')
            source = archive.extractfile(member)
            if source is None:
                raise ValueError('unreadable metadata entry')
            try:
                value = json.loads(source.read(MAX_METADATA_BYTES + 1))
            finally:
                source.close()
            files = value.get('files') if isinstance(value, dict) else None
            if files is None and isinstance(value, dict):
                files = []
            if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
                raise ValueError('invalid metadata source list')
            for item in files:
                item = item.strip()
                # Match the historical mirror's selection without contacting
                # source URLs; local paths or non-HTTP metadata are ignored.
                if item.startswith(('http://', 'https://')):
                    urls.add(item)
    if not count:
        raise ValueError('parsed snapshot has no source metadata')
    return urls


def compare(archive_id: int, source_revision: str, dataset_revision: str, raw_manifest: bytes,
            source_urls: set[str]) -> dict:
    if not 0 <= archive_id <= 31:
        raise ValueError('archive number must be between 0 and 31')
    if any(not re.fullmatch(r'[0-9a-f]{40}', str(value or '')) for value in (source_revision, dataset_revision)):
        raise ValueError('source and mirror must both be pinned revisions')
    if not raw_manifest or len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise ValueError('source mirror manifest has invalid size')
    manifest = json.loads(raw_manifest)
    if (not isinstance(manifest, dict) or manifest.get('version') != 1
            or not isinstance(manifest.get('files'), dict)):
        raise ValueError('invalid BHA source mirror manifest')
    rows = manifest['files']
    for url in source_urls & rows.keys():
        entry = rows[url]
        if (not isinstance(entry, dict) or type(entry.get('archive_id')) is not int
                or not 0 <= entry['archive_id'] <= 31 or type(entry.get('bytes')) is not int or entry['bytes'] <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256', '')))):
            raise ValueError('invalid source mirror entry')
        path = entry.get('path')
        if (not isinstance(path, str) or not path.startswith(f"archives{entry['archive_id']}/")
                or any(part in {'', '.', '..'} for part in path.split('/')) or '\\' in path
                or any(ord(char) < 32 for char in path)):
            raise ValueError('invalid source mirror path')
    missing = source_urls - rows.keys()
    return {'version': 1, 'kind': 'bha-source-mirror-readonly-report', 'archive': archive_id,
            'upstream_parsed_revision': source_revision, 'mirror_dataset_revision': dataset_revision,
            'mirror_manifest_sha256': hashlib.sha256(raw_manifest).hexdigest(), 'source_urls': len(source_urls),
            'matched_manifest_urls': len(source_urls) - len(missing), 'missing_manifest_urls': len(missing),
            'source_extensions': dict(sorted(Counter(PurePosixPath(urlparse(url).path).suffix.lower() or '(none)'
                                                     for url in source_urls).items())),
            'remote_writes': 0, 'mirror_requested': False,
            'scope': 'pinned parsed metadata and mirror manifest keys only; object bytes and later source changes not checked'}


def inspect(archive_id: int, github_token: str, hf_token: str):
    if not github_token or not hf_token:
        raise RuntimeError('GitHub and HF credentials are required')
    if not 0 <= archive_id <= 31:
        raise ValueError('archive number must be between 0 and 31')
    api = HfApi(token=hf_token)
    source_revision = audit.parsed_commit(SOURCE_OWNER, archive_id, github_token)
    dataset_revision = api.repo_info(DATASET, repo_type='dataset').sha
    if not re.fullmatch(r'[0-9a-f]{40}', str(dataset_revision or '')):
        raise ValueError('mirror dataset revision is not pinned')
    local_manifest = Path(api.hf_hub_download(DATASET, 'manifest.json', repo_type='dataset', revision=dataset_revision))
    if local_manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('source mirror manifest exceeds inspection limit')
    with tempfile.TemporaryDirectory(prefix='bha-source-readonly-') as directory:
        snapshot = audit.download_snapshot(SOURCE_OWNER, archive_id, source_revision, Path(directory))
        source_urls = urls_in_snapshot(snapshot)
    result = compare(archive_id, source_revision, dataset_revision, local_manifest.read_bytes(), source_urls)
    result['upstream_head_changed_during_check'] = audit.parsed_commit(SOURCE_OWNER, archive_id, github_token) != source_revision
    result['mirror_head_changed_during_check'] = api.repo_info(DATASET, repo_type='dataset').sha != dataset_revision
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = inspect(args.archive, os.environ.get('GH_PAT', ''), os.environ.get('HF_TOKEN', ''))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
