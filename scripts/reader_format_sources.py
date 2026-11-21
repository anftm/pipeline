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
    from . import reader_format_backlog as backlog, reader_ordinary_boost as boost
except ImportError:
    import reader_format_backlog as backlog
    import reader_ordinary_boost as boost


def _identities(records):
    # Fetch uses SKIP_SIZES; publication also normalizes retired OCR flags.
    # Neither affects the exact repo/path/extension/revision inventory.
    return {key: {field: value for field, value in record.items() if field != 'bytes'}
            for key, record in records.items()}


def _boosted_prefix(records, extension, limit, budget):
    shards = [[] for _ in range(boost.SHARDS)]
    skipped = 0
    for record in records:
        # Never spend an unverified or oversized slot in a boosted batch.
        # Those sources are left to ordinary runs; there is no byte-cap fallback.
        if not 0 < record['bytes'] <= budget:
            skipped += 1
            continue
        shards[backlog.reader_stream_index.shard_for(record['key'], boost.SHARDS)].append(record)
    # The image worker sorts by bytes then repo/path. Media sorts by
    # extension then repo/path; all these media rows are MP3.
    order = ((lambda row: (row['bytes'], row['repo'], row['path'])) if extension == 'jpg'
             else (lambda row: (row['repo'], row['path'])))
    selected = []
    for rows in shards:
        remaining = budget
        for record in sorted(rows, key=order)[:limit]:
            if record['bytes'] > remaining:
                break
            selected.append(record)
            remaining -= record['bytes']
    if not selected:
        raise ValueError('no actionable sources with a positive pinned size within the boost budget')
    return selected, skipped


def prepare(*, extension, generation, search_data, revisions, output, hub, api, fs, client,
            profile_proof=None, include_profile_upgrades=True, boost_limit=None):
    """Inspect once, validate the full fetch, and write a new compact subset."""
    if extension not in backlog.EXTENSIONS:
        raise ValueError('requires one supported Reader extension')
    budget = boost.byte_budget(extension, boost_limit) if boost_limit is not None else None
    if budget is not None and profile_proof is not None:
        raise ValueError('ordinary boost cannot request profile work')
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
    actionable_states = {'not_mapped', 'content_changed'}
    if budget is None:
        actionable_states.add('profile_outdated')
    for key, record in records.items():
        if record['extension'] != extension:
            continue
        row = details['rows'].get(key)
        if (not row or row.get('state') not in actionable_states
                or row.get('recorded_failure') or row.get('pending_shard_checkpoint')):
            continue
        if row.get('key') != key or row.get('source_revision') != record['revision']:
            raise ValueError('classified source differs from pinned record')
        # Ordinary unchanged files remain excluded. A reviewed old chapter
        # profile is a separate reason to rebuild, not a source-content change.
        selected.append(record)

    if budget is not None:
        # Fetch may omit sizes (MP3) or disagree with the published corpus.
        # Use exactly the sizes the producer will see in its plan artifact.
        selected, skipped = _boosted_prefix([details['records'][row['key']] for row in selected],
                                            extension, boost_limit, budget)
    requests = {(row['repo'], row['revision'], row['path']): row for row in selected}
    metadata = backlog._metadata(api, requests)
    for request, record in requests.items():
        value = metadata.get(request)
        if value is None or not (backlog._lfs_identity(value) or backlog._blob_identity(value)):
            raise ValueError('selected source lacks exact pinned file identity: ' + request[2])
        if budget is not None:
            size = backlog._item_field(value, 'size')
            if type(size) is not int or size != record['bytes']:
                raise ValueError('selected source size differs from pinned file metadata: ' + request[2])

    selected_by_key = {record['key']: record for record in selected}
    # Preserve dictionaries and row order; never re-encode/split away repositories
    # needed by the full revision guard. source_records preserves decoder order.
    filtered = {**payload, 'rc': [row if budget is None else [*row[:4], selected_by_key[key]['bytes'], row[5]]
                                  for key, row in zip(records, payload['rc']) if key in selected_by_key]}
    profile_keys = {key: details['rows'][key]['previous_source_sha256'] for key in sorted(selected_by_key)
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
            'source_revisions': source_revisions, 'remote_writes': 0,
            **({'boost_limit': boost_limit, 'source_byte_budget_per_shard': budget,
                'skipped_unknown_or_oversized': skipped} if budget is not None else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extension', required=True, choices=sorted(backlog.EXTENSIONS))
    parser.add_argument('--generation', required=True)
    parser.add_argument('--search-data', required=True, type=Path)
    parser.add_argument('--revisions', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--profile-proof', type=Path)
    parser.add_argument('--boost-limit', type=int)
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
