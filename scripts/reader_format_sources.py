"""Prepare a new converter corpus containing only verified actionable keys.

Run after fetch/source guard and before planning, then move the new file onto
output/search_data.json. Keep state/commits.json intact for the queued worker's
source guard. This subset is converter input, never a publication corpus: the
published manifest and record count are checked against the full capture only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from . import reader_format_backlog as backlog
except ImportError:
    import reader_format_backlog as backlog


def _identities(records):
    # Fetch uses SKIP_SIZES; publication also normalizes retired OCR flags.
    # Neither affects the exact repo/path/extension/revision inventory.
    return {key: {field: value for field, value in record.items() if field != 'bytes'}
            for key, record in records.items()}


def prepare(*, extension, generation, search_data, revisions, output, hub, api, fs, client,
            profile_proof=None, include_profile_upgrades=True):
    """Inspect once, validate the full fetch, and write a new compact subset."""
    if extension not in backlog.EXTENSIONS:
        raise ValueError('requires one supported Reader extension')
    if not isinstance(generation, str) or not backlog.SHA.fullmatch(generation):
        raise ValueError('invalid published search generation')
    search_data, revisions, output = Path(search_data), Path(revisions), Path(output)
    if (output.exists() or output.is_symlink()
            or output.resolve() in {search_data.resolve(), revisions.resolve()}):
        raise ValueError('filtered output must be a new distinct path')
    payload = backlog.search_generation._read_json_file(search_data)
    source_revisions = backlog.search_generation._read_json_file(revisions)
    records = backlog.source_records(payload, source_revisions)
    details = {}
    summary, _epub = backlog.inspect(hub=hub, api=api, fs=fs, client=client,
                                     scope='all', details_out=details,
                                     include_profile_upgrades=include_profile_upgrades)
    if summary['search_generation'] != generation:
        raise ValueError('published search generation differs from producer pin')
    if source_revisions != details['source_revisions']:
        raise ValueError('fetched revisions differ from published source revisions')
    if _identities(records) != _identities(details['records']):
        raise ValueError('fetched corpus differs from complete published source inventory')

    selected = []
    for key, record in records.items():
        if record['extension'] != extension:
            continue
        row = details['rows'].get(key)
        if (not row or row.get('state') not in {'not_mapped', 'content_changed', 'profile_outdated'}
                or row.get('recorded_failure') or row.get('pending_shard_checkpoint')):
            continue
        if row.get('key') != key or row.get('source_revision') != record['revision']:
            raise ValueError('classified source differs from pinned record')
        # Ordinary unchanged files remain excluded. A reviewed old chapter
        # profile is a separate reason to rebuild, not a source-content change.
        selected.append(record)

    requests = {(record['repo'], record['revision'], record['path']) for record in selected}
    metadata = backlog._metadata(api, requests)
    for request in requests:
        value = metadata.get(request)
        if value is None or not (backlog._lfs_identity(value) or backlog._blob_identity(value)):
            raise ValueError('selected source lacks exact pinned file identity: ' + request[2])

    selected_keys = {record['key'] for record in selected}
    # Preserve dictionaries and row order; never re-encode/split away repositories
    # needed by the full revision guard. source_records preserves decoder order.
    filtered = {**payload, 'rc': [row for key, row in zip(records, payload['rc']) if key in selected_keys]}
    profile_keys = {key: details['rows'][key]['previous_source_sha256'] for key in sorted(selected_keys)
                    if details['rows'][key]['state'] == 'profile_outdated'}
    if profile_proof is not None:
        if extension != 'chm' or Path(profile_proof).resolve() in {
                output.resolve(), search_data.resolve(), revisions.resolve()}:
            raise ValueError('profile proof requires a distinct CHM output path')
        with Path(profile_proof).open('x', encoding='utf8') as stream:
            json.dump({'search_generation': generation, 'reader_generation': summary['reader_generation'],
                       'profile_keys': profile_keys}, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8') as stream:
        json.dump(filtered, stream, ensure_ascii=False, separators=(',', ':'))
        stream.write('\n')
    return {'version': 1, 'kind': 'reader-format-sources', 'extension': extension,
            'search_generation': generation, 'reader_generation': summary['reader_generation'],
            'output': str(output), 'source_records': len(records), 'selected_records': len(selected),
            'source_revisions': source_revisions, 'remote_writes': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extension', required=True, choices=sorted(backlog.EXTENSIONS))
    parser.add_argument('--generation', required=True)
    parser.add_argument('--search-data', required=True, type=Path)
    parser.add_argument('--revisions', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--profile-proof', type=Path)
    args = parser.parse_args(argv)
    token = os.environ.get('HF_TOKEN')
    with backlog.httpx.Client(timeout=90, follow_redirects=True) as client:
        result = prepare(**vars(args), hub=backlog.HubBucketStore(), api=backlog.HfApi(token=token),
                         fs=backlog.HfFileSystem(token=token), client=client,
                         include_profile_upgrades=os.getenv('READER_CHM_PROFILE_REBUILD_ENABLED') == 'true')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
