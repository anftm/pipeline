"""Manual dispatcher for the one sealed h.PNG migration, never a GC command."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import reader_image_history_archive as archive
from . import reader_image_history_fence as fence
from . import reader_image_history_hpng as pilot
from .reader_image_history_package import load
from .reader_image_history_preflight import ReadOnlyS3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('verify', 'stage-parent', 'publish-legacy', 'promote', 'observe-promotion',
                                          'verify-consumers'))
    parser.add_argument('--local', type=Path, help='an independently verified copy of the exact sealed package')
    parser.add_argument('--window-sha256')
    parser.add_argument('--external-writers-stopped', action='store_true')
    parser.add_argument('--resume-indexes', action='store_true')
    parser.add_argument('--resume-legacy', action='store_true')
    args = parser.parse_args(argv)
    if (args.phase == 'verify' and (args.window_sha256 or args.external_writers_stopped)
            or args.phase != 'verify' and not (args.window_sha256 and args.external_writers_stopped)
            or args.phase != 'publish-legacy' and (args.resume_indexes or args.resume_legacy)
            or args.resume_legacy and not args.resume_indexes):
        parser.error('select one explicit phase with its required window and recovery options')

    import httpx

    s3 = ReadOnlyS3()
    if args.local is None:
        reviewed = archive.download(s3._client)
    else:
        with args.local.open('rb') as raw:
            reviewed = load(raw, expected_sha256=archive.EXPECTED_SHA256)
    with httpx.Client(follow_redirects=True, timeout=120) as http:
        op = pilot.Operator.from_env(reviewed, http=http)
        if args.phase == 'verify':
            result = op.verify()
        else:
            intent = reviewed.transition.canonical
            guard = fence.WindowGuard(s3, key=intent.key, operation=intent.operation,
                                      reviewed_sha256=reviewed.sha256, receipt_sha256=args.window_sha256,
                                      external_writers_stopped=args.external_writers_stopped)
            guard('cli:before')
            if args.phase == 'stage-parent':
                result = op.stage_parent(guard=guard)
            elif args.phase == 'publish-legacy':
                result = op.publish_legacy(guard=guard, resume_indexes=args.resume_indexes, resume_legacy=args.resume_legacy)
                result = {k: result[k] for k in ('generation', 'claim_id', 'seal', 'catalog', 'sidecar', 'indexes',
                                                 'reader_consumers_verified', 'deletion_authorized')}
            elif args.phase in ('promote', 'observe-promotion'):
                result = op.promote(guard=guard, observe_only=args.phase == 'observe-promotion')
            else:
                result = op.observe_v3(guard=guard)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=_serialise))


def _serialise(value):
    if not is_dataclass(value):
        raise TypeError('unexpected image migration result')
    return asdict(value)


if __name__ == '__main__':
    main()
