"""Default-off admission limits for ordinary automatic JPG and MP3 batches.

The daily controller requests a higher limit only for an explicit one-batch
canary. Producer planning caps each shard's ordered prefix using immutable
source metadata. Conversion and publication are unchanged.
"""
from __future__ import annotations

import os
import re
import sys

SHARDS = 8
STANDARD_LIMIT = 3
# (attempts, source bytes per shard, repository opt-in)
POLICIES = {
    'jpg': (12, 256 * 1024**2, 'READER_JPG_BATCH_BOOST_ENABLED'),
    'mp3': (8, 512 * 1024**2, 'READER_MP3_BATCH_BOOST_ENABLED'),
}


def batch_limit(extension, verified_backlog):
    # Size from the refreshed count without assuming that shards are balanced.
    # The producer will cap each shard independently using its actual rows.
    per_shard = (verified_backlog + SHARDS - 1) // SHARDS
    return min(POLICIES[extension][0], max(STANDARD_LIMIT, per_shard))


def byte_budget(extension, limit):
    policy = POLICIES.get(extension)
    if not policy or type(limit) is not int or not STANDARD_LIMIT < limit <= policy[0]:
        raise ValueError('boost limit outside the ordinary JPG/MP3 ceiling')
    return policy[1]


def validate_workflow(extension, env):
    """Compose with the producer's current-main and published source guards."""
    raw_limit = env.get('LIMIT', '')
    if not re.fullmatch(r'[1-9][0-9]?', raw_limit):
        raise ValueError('apply requires a positive bounded batch count')
    byte_budget(extension, int(raw_limit))
    if (env.get(POLICIES[extension][2]) != 'true' or env.get('BOOST_CANARY') != 'true'
            or env.get('EXTENSION') != extension
            or (extension == 'jpg' and env.get('SHARD') != 'all')
            or (extension == 'mp3' and env.get('KIND') != 'audio')
            or env.get('APPLY') != 'true' or env.get('RETRY_FAILURES') != 'false'
            or env.get('DEFER_UNIFY') != 'true'
            or not re.fullmatch(r'[a-f0-9]{32}', env.get('AUTOMATION_ID', ''))
            or env.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or env.get('GITHUB_REF') != 'refs/heads/main'
            or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_ACTOR') != 'github-actions[bot]'
            or env.get('GITHUB_RUN_ATTEMPT') != '1'):
        raise ValueError('boost requires an enabled ordinary first-attempt automatic producer')


if __name__ == '__main__':
    try:
        validate_workflow(sys.argv[1], os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
