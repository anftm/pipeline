"""Select producer credentials without changing the shared secret mapping.

One previously expired account has an independently stored replacement. The
override is deliberately bound to that exact repository and supplied only by
trusted workflows. Missing replacements fall back to the existing map; this
module never validates permissions or treats a failed response as isolation.
"""
import json
import os


OVERRIDE_REPOSITORY = 'ambrossee768/pdf-pipeline'
OVERRIDE_VARIABLE = 'GC_AMBROSSEE_MAINTENANCE_TOKEN'


def token_for(repo):
    try:
        tokens = json.loads(os.environ.get('GC_PRODUCER_TOKENS_JSON', ''))
    except (TypeError, ValueError) as error:
        raise RuntimeError('invalid maintenance credential mapping') from error
    if not isinstance(tokens, dict):
        raise RuntimeError('invalid maintenance credential mapping')
    override = os.environ.get(OVERRIDE_VARIABLE)
    if repo == OVERRIDE_REPOSITORY and override:
        return override
    token = tokens.get(repo)
    if not isinstance(token, str) or not token:
        raise RuntimeError('incomplete maintenance credentials')
    return token
