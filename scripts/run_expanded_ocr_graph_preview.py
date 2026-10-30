#!/usr/bin/env python3
"""Run an expanded OCR/PS graph simulation under durable producer isolation."""
import json
import os
from pathlib import Path
import httpx

try:
    from . import expanded_ocr_retirement as expanded, reader_gc_maintenance as maintenance
    from .reader_gc_fence import GitHubProducerFence
    from .reader_gc_store import CcrdCollectionStore
except ImportError:
    import expanded_ocr_retirement as expanded
    import reader_gc_maintenance as maintenance
    from reader_gc_fence import GitHubProducerFence
    from reader_gc_store import CcrdCollectionStore


REPO = 'anftm/pipeline'
WORKFLOW = REPO + '/.github/workflows/expanded-ocr-graph-preview.yml@refs/heads/main'
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('output/expanded-ocr-preview')


def assert_context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW or not os.environ.get('GITHUB_RUN_ID')):
        raise RuntimeError('expanded OCR graph preview requires its exact central workflow')


def main():
    assert_context()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    inventory = expanded.inventory(ROOT / 'config/expanded-ocr-ps-inventory-20261030.json')
    result = {'kind': 'expanded-ocr-graph-preview-run', 'complete': False,
              'target_count': len(inventory['targets']), 'target_bytes': inventory['bytes'],
              'data_deletions': 0, 'control_reference_changes': 0, 'phase': 'starting'}

    def checkpoint(phase, **extra):
        result.update(phase=phase, **extra)
        (OUTPUT / 'result.json').write_bytes(expanded.encode(result))
        print(json.dumps({'phase': phase, **extra}, ensure_ascii=False), flush=True)

    def isolated(_state):
        store = CcrdCollectionStore()
        fence = GitHubProducerFence()
        try:
            fence.check()
            checkpoint('fresh-preflight')
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                plan = expanded.prepare(store, client, inventory['targets'])
            (OUTPUT / 'plan.json').write_bytes(expanded.encode(plan))
            checkpoint('four-bucket-simulation')
            proof = expanded.simulate(store, plan, fence, workers=8, deadline_minutes=300,
                                      omit_internal_target_manifests=True,
                                      progress=lambda stage, **counts: print(
                                          json.dumps({'scan': stage, **counts}, ensure_ascii=False), flush=True))
            result['simulation'] = proof
            checkpoint('complete', complete=True)
            return result
        except Exception as error:
            checkpoint('failed', error_type=type(error).__name__, error=str(error))
            raise

    checkpoint('starting')
    maintenance.run(OUTPUT / 'maintenance.json', isolated_callback=isolated,
                    suppress_resume_dispatch=True)


if __name__ == '__main__':
    main()
