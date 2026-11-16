"""Run the reviewed obsolete-PDF reference edits inside restoring maintenance."""
import argparse
import json
import os
from pathlib import Path

import httpx

from . import reader_gc_maintenance as maintenance, reader_gc_graph as graph
from . import reader_gc_collect as collector, reader_gc_chm_window
from . import reader_obsolete_pdf as preparation, reader_obsolete_pdf_apply as operation
from .reader_obsolete_pdf_store import ObsoleteReferenceStore
from .reader_bucket_store import HubBucketStore
from .reader_gc_store import CollectionStore
from .reader_gc_fence import GitHubProducerFence
from . import publish_reader_v3 as publication


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_bytes())
    if set(config) != {'inventory', 'exact_copies', 'manifest_inventory', 'ccrd_original'}:
        raise ValueError('exact obsolete PDF preparation inputs required')
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != 'anftm/pipeline/.github/workflows/reader-maintenance.yml@refs/heads/main'
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'):
        raise RuntimeError('obsolete retirement requires the central maintenance first attempt')
    result = {}

    def isolated(state):
        nonlocal result
        from .reader_obsolete_pdf_references import verify as verify_incoming
        fence = GitHubProducerFence()
        fence.check()
        reads = CollectionStore()
        hub = HubBucketStore()
        reader_gc_chm_window.require_clear(reads)
        pointer, catalog = publication.current(reads)
        projection = publication.project_sidecar(publication.pinned_base(reads, catalog, None), catalog)

        def metadata(bucket, path):
            rows = list(hub.api.get_bucket_paths_info(bucket, [path]))
            if len(rows) != 1:
                raise ValueError('PDF metadata absent or ambiguous')
            return {'bucket': bucket, 'path': path, 'size': rows[0].size, 'xet_hash': rows[0].xet_hash}

        with httpx.Client(timeout=120, follow_redirects=True) as client:
            def reprepare():
                observations = {site: publication.observe_consumer(reads, site, client)
                                for site in ('hf', 'pages')}
                return preparation.prepare(reads, **config,
                    ccrd_root=('vomebook/ccrd-index', operation.CCRD_ROOT),
                    observations=observations, projection=projection, metadata=metadata)
            plan = reprepare()
            if plan['blockers']:
                raise ValueError('obsolete PDF preparation blocked')
            reference = graph.ReferenceGraph(reads, workers=8)
            report = reference.build()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            try:
                proof = verify_incoming(reference, report, plan)
            except Exception as error:
                result = {'reference_proof': getattr(error, 'proof', None),
                          'error': type(error).__name__, 'operation': None}
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
                raise
            result = {'reference_proof': proof, 'operation': None}
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            collector.verify_roots(reads, reference)
            collector.verify_graph_documents(reads, reference)
            writes = ObsoleteReferenceStore(hub, plan['mutations'])

            def check_window():
                fence.check()
                if publication.current(reads)[0] != pointer:
                    raise RuntimeError('Reader generation changed in maintenance')
                return True
            def checkpoint(outcome):
                result['operation'] = outcome
                state['obsolete_pdf_operation'] = outcome
                maintenance.journal(args.journal, state)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))

            result['operation'] = operation.apply(writes, plan, reprepare, check_window,
                checkpoint=checkpoint, owner={'repository': os.environ['GITHUB_REPOSITORY'],
                    'run_id': os.environ['GITHUB_RUN_ID'], 'attempt': os.environ['GITHUB_RUN_ATTEMPT']})
            if result['operation']['outcome'] != 'applied':
                raise RuntimeError('partial reference update; inspect durable intent before any retry')
            check_window()
            after = operation.inspect(writes, result['operation']['intent_ref'])
            if not all(row['outcome'] == 'after' for row in after['roots']):
                raise RuntimeError('collective after-state mismatch')
            after_graph = graph.ReferenceGraph(reads, workers=8)
            after_report = after_graph.build()
            if not after_report.get('graph_complete') or after_report.get('blockers'):
                raise RuntimeError('after-state reference graph incomplete')
            observations = {site: publication.observe_consumer(reads, site, client)
                            for site in ('hf', 'pages')}
            if any(not row.get('projection_verified') or row.get('generation') != plan['generation']
                   for row in observations.values()):
                raise RuntimeError('after-state consumer proof failed')
            current_projection = publication.project_sidecar(publication.pinned_base(reads, catalog, None), catalog)
            if current_projection != projection:
                raise RuntimeError('active reading routes changed')
            for replacement in plan['replacement_proofs']:
                pdf = replacement['pdf']
                if metadata(pdf['bucket'], pdf['path']) != pdf['metadata']:
                    raise RuntimeError('active PDF object identity changed')
            collector.verify_roots(reads, after_graph)
            collector.verify_graph_documents(reads, after_graph)
            check_window()
            result['acceptance'] = {'roots': after, 'graph_complete': True,
                                    'observations': observations, 'active_pdfs_unchanged': True}
            state['obsolete_pdf_acceptance'] = result['acceptance']
            maintenance.journal(args.journal, state)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result
    # Reference-only edits: both original and replacement PDF bytes remain.
    # On partial mutation restore writers, retain the exact intent, and stop;
    # no object deletion is authorized by this operation.
    maintenance.run(args.journal, isolated_callback=isolated,
                    suppress_resume_dispatch=True, preserve_queued_runs=True)


if __name__ == '__main__':
    main()
