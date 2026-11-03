#!/usr/bin/env python3
"""Verify BHA's administrative status endpoint without starting a reindex."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


URL = 'https://vomebook-bha-search.hf.space/api/reindex/status'
MAX_BYTES = 1024 * 1024


def check(token, *, open_url=urlopen):
    if not token:
        raise RuntimeError('BHA_REINDEX_TOKEN is not configured')
    request = Request(URL, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
        'User-Agent': 'pipeline-bha-reindex-readonly-check',
    }, method='GET')
    try:
        with open_url(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError('BHA status request failed: HTTP ' + str(response.status))
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        raise RuntimeError('BHA status request failed: HTTP ' + str(status)) from None
    except (URLError, OSError, TimeoutError):
        raise RuntimeError('BHA status request failed: network error') from None
    if len(raw) > MAX_BYTES:
        raise RuntimeError('BHA status response is too large')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError('BHA status response is not valid JSON') from None
    if not isinstance(data, dict) or not isinstance(data.get('archive_revisions'), dict):
        raise RuntimeError('BHA status response has an invalid schema')
    if data.get('index_status') != 'ready' or data.get('switch_pending') is not False:
        raise RuntimeError('BHA index is not ready or is switching')
    return {'authenticated': True, 'index_status': 'ready', 'switch_pending': False,
            'archive_count': len(data['archive_revisions'])}


def main():
    result = check(os.environ.get('BHA_REINDEX_TOKEN', ''))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
