#!/usr/bin/env python3
"""Report pdf-pages-v2 candidates from an existing read-only full reference graph.

Incomplete graphs, missing cross-bucket inventory and unstable roots yield no
candidates. This module does not delete, write state, or authorize deletion.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

try:
    from . import reader_gc_graph as graph, shared
except ImportError:
    import reader_gc_graph as graph
    import shared


BUCKET = shared.PDF_PAGES_BUCKET


def report_from_graph(report: dict, sizes: dict | None = None) -> dict:
    if not isinstance(report, dict) or not isinstance(report.get('buckets'), dict):
        raise ValueError('invalid graph report')
    missing = sorted(set(graph.BUCKETS) - set(report['buckets']))
    complete = (report.get('version') == 1 and report.get('mode') == 'report-only'
                and report.get('graph_complete') is True and not missing and not report.get('blockers'))
    data = report['buckets'].get(BUCKET, {})
    if not isinstance(data, dict):
        raise ValueError('invalid page inventory')
    candidates = data.get('candidates', []) if complete else []
    if not isinstance(candidates, list) or any(not isinstance(path, str) for path in candidates):
        raise ValueError('invalid page candidate paths')
    sizes = sizes or {}
    groups = {}
    for prefix in ('derived/', 'objects/'):
        paths = sorted(path for path in candidates if path.startswith(prefix))
        counts = Counter(path.rsplit('/', 1)[-1] for path in paths)
        groups[prefix] = {'files': len(paths),
                          'known_bytes': sum(sizes.get((BUCKET, path), 0) for path in paths),
                          'sizes_complete': all((BUCKET, path) in sizes for path in paths),
                          'by_name': dict(sorted(counts.items())), 'paths': paths}
    return {'version': 1, 'kind': 'pdf-page-cleanup-report', 'bucket': BUCKET,
            'source_report_trusted': False, 'graph_complete': complete, 'missing_inventories': missing,
            'blockers': report.get('blockers', []), 'scanned_at': report.get('scanned_at'),
            'groups': groups, 'deletion_authorized': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        value = json.loads(args.graph_report.read_text(encoding='utf-8'))
        report = report_from_graph(value)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'graph_complete': report['graph_complete'], 'deletion_authorized': False,
                          'counts': {key: value['files'] for key, value in report['groups'].items()},
                          'output': str(args.output)}, ensure_ascii=False))
        return 0 if report['graph_complete'] else 1
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
