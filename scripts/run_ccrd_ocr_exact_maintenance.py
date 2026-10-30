#!/usr/bin/env python3
"""Run the exact CCRD OCR retirement inside durable producer isolation."""
import argparse
from pathlib import Path
import httpx

try:
    from . import ccrd_ocr_exact_retirement as exact, reader_gc_maintenance as maintenance
except ImportError:
    import ccrd_ocr_exact_retirement as exact
    import reader_gc_maintenance as maintenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delete', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--maintenance-journal', type=Path, required=True)
    args = parser.parse_args()
    if args.resume and not args.delete:
        parser.error('--resume requires --delete')
    exact.assert_writer()
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        return maintenance.run(args.maintenance_journal, suppress_resume_dispatch=True, isolated_callback=lambda _:
            exact.run(args.output, client, delete=args.delete, resume=args.resume))


if __name__ == '__main__':
    main()
