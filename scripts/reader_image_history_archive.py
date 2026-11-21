"""Read the one reviewed h.PNG package from its exact recovery-only key."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from . import reader_image_history_package as package
from .reader_image_history_preflight import ReadOnlyS3
from .reader_image_history_parent import BUCKET
from .reader_bucket_store import retry_s3

EXPECTED_SHA256 = 'a45f0bab0e3d609f7959eddb70ae840f2f6212cb14571078865049c26f322bf8'
EXPECTED_BYTES = 20992007
KEY = 'reader-index/gc/image-history-inputs/v1/' + EXPECTED_SHA256 + '.zip'


def download(client):
    """Never fall back to a different version or a local, unsealed package."""
    def read():
        response = client.get_object(Bucket=BUCKET, Key=KEY)
        body = response['Body']
        try:
            if (response['ResponseMetadata']['HTTPStatusCode'] != 200
                    or response['ContentLength'] != EXPECTED_BYTES):
                raise ValueError('review archive response differs from the sealed h.PNG package')
            raw = body.read(EXPECTED_BYTES + 1)
            if len(raw) != EXPECTED_BYTES:
                raise OSError('short review archive')
            return package.load(io.BytesIO(raw), expected_sha256=EXPECTED_SHA256)
        finally:
            body.close()
    return retry_s3(read, attempts=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', type=Path, help='inspect a local copy of the same sealed package')
    args = parser.parse_args()
    if args.local is not None:
        with args.local.open('rb') as raw:
            reviewed = package.load(raw, expected_sha256=EXPECTED_SHA256)
    else:
        reviewed = download(ReadOnlyS3()._client)
    print(json.dumps({'operation': reviewed.transition.canonical.operation, 'reviewed_sha256': reviewed.sha256,
                      'legacy_generation': reviewed.legacy_generation,
                      'reader_generation': json.loads(reviewed.pointer_target)['generation'],
                      'applied': False}, sort_keys=True))


if __name__ == '__main__':
    main()
