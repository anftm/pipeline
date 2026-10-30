#!/usr/bin/env python3
"""Preview the complete CHM EPUB withdrawal graph without remote changes."""
import json
import os
from pathlib import Path

import httpx

try:
    from . import chm_epub_publication_preflight as chm, chm_epub_withdrawal_preview as preview
    from . import reader_gc_collect as collector, reader_gc_maintenance as maintenance
    from .reader_gc_fence import GitHubProducerFence, assert_workflow_context
    from .reader_gc_store import CcrdCollectionStore
except ImportError:
    import chm_epub_publication_preflight as chm
    import chm_epub_withdrawal_preview as preview
    import reader_gc_collect as collector
    import reader_gc_maintenance as maintenance
    from reader_gc_fence import GitHubProducerFence, assert_workflow_context
    from reader_gc_store import CcrdCollectionStore


WORKFLOW = '.github/workflows/chm-epub-graph-preview.yml'
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('output/chm-epub-graph-preview')


def main():
    assert_workflow_context(WORKFLOW)
    rows = preview.load_inventory(ROOT / 'config/chm-history-inventory-20261030.json')['targets']
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result = {'kind': 'chm-epub-graph-preview', 'complete': False, 'target_count': len(rows),
              'data_deletions': 0, 'control_reference_changes': 0}

    def checkpoint(phase, **extra):
        result.update(phase=phase, **extra)
        (OUTPUT / 'result.json').write_text(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'phase': phase, **extra}, ensure_ascii=False), flush=True)

    def isolated(_state):
        store, fence = CcrdCollectionStore(), GitHubProducerFence()
        try:
            fence.check()
            checkpoint('verify-both-readers')
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                identity, observations = collector.observe_projections(store, client)
            generations = {site: observations['receipts'][site]['generation'] for site in ('hf', 'pages')}
            plan = chm.plan_chm_withdrawal(store, ROOT / 'config/chm-history-inventory-20261030.json',
                                           acknowledged_generations=generations)
            if identity['digest'] != plan['catalog_sha256_before']:
                raise ValueError('Reader catalog changed during planning')
            result['plan'] = {key: value for key, value in plan.items() if key not in ('overrides', 'immutable')}
            checkpoint('simulated-four-bucket-closure')
            proof = chm.simulate_complete_withdrawal(store, plan['overrides'], rows, fence,
                                                       workers=8, deadline_minutes=300,
                                                       immutable=plan['immutable'])
            result['simulation'] = proof
            checkpoint('complete', complete=True)
            return result
        except Exception as error:
            checkpoint('failed', error_type=type(error).__name__, error=str(error))
            raise

    checkpoint('starting')
    maintenance.run(OUTPUT / 'maintenance.json', isolated_callback=isolated,
                    suppress_resume_dispatch=True, preserve_queued_runs=True)


if __name__ == '__main__':
    main()
