"""Exact former GitHub execution identities, not bucket-writer isolation proof."""
from pathlib import Path

from . import reader_retired_repository_receipts as receipts

DIRECTORY = Path(__file__).resolve().parents[1] / 'config/producer-migration-audit-20261103/receipts'
PINS = {
    'ambrossee768/pipeline': (1401656391, '97f2b7a2a5ab52066795b2c421f14bbf2e58d219ece7c50cb1e7ccd6a37cd05c'),
    'vomebook/pipeline': (1390570183, '61051315aefe28e4cb489efdf16d7e86a0b4c0e303f50694fdded4dee52c0877'),
}


def verified_deletions(additional, active):
    """Recognise reviewed deletion of the exact two historical add-on IDs.

    Never exempt an explicit current producer or a different/unknown identity.
    These receipts are checked from pinned code, never inferred from an API 404.
    """
    active = {repo.casefold() for repo in active}
    results = []
    for name, (repo_id, digest) in PINS.items():
        if name.casefold() in active or name not in additional:
            continue
        if type(additional[name]) is not int or additional[name] != repo_id:
            raise ValueError('historical repository ID changed: ' + name)
        with (DIRECTORY / (digest + '.json')).open('rb') as source:
            raw = source.read(receipts.MAX_RECEIPT_BYTES + 1)
        results.append(receipts.verify(raw, name, repo_id, expected_sha256=digest))
    return results
