"""S3 adapter for immutable retirement audit objects; no GC activation.

Only the exact content-addressed audit namespace can be read or created. Writes
use If-None-Match with SDK retries disabled; ambiguous outcomes are reconciled
by the separate audit publication model, never retried here.
"""
from __future__ import annotations

import hashlib
import os

try:
    from . import reader_producer_audit_publish as publication
except ImportError:
    import reader_producer_audit_publish as publication


BUCKET = 'reader-assets-v2'
ENDPOINT = 'https://s3.hf.co/vomebook'


class AuditStore:
    def __init__(self, client=None):
        if client is not None:
            self.client = client
            return
        access, secret = os.environ.get('HF_S3_ACCESS_KEY_ID'), os.environ.get('HF_S3_SECRET_ACCESS_KEY')
        if not access or not secret:
            raise RuntimeError('audit S3 credentials unavailable')
        import boto3
        from botocore.config import Config
        self.client = boto3.client('s3', endpoint_url=ENDPOINT, aws_access_key_id=access,
            aws_secret_access_key=secret, config=Config(region_name='us-east-1',
                s3={'addressing_style': 'path'}, retries={'total_max_attempts': 1},
                connect_timeout=10, read_timeout=30, request_checksum_calculation='when_required',
                response_checksum_validation='when_required'))

    @staticmethod
    def check_key(path):
        if not isinstance(path, str) or not publication.PATTERN.fullmatch(path):
            raise ValueError('audit S3 path outside fixed content-addressed namespace')

    def read(self, path):
        self.check_key(path)
        try:
            result = self.client.get_object(Bucket=BUCKET, Key=path)
        except Exception as error:
            response = getattr(error, 'response', {}) or {}
            status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            code = str(response.get('Error', {}).get('Code', ''))
            if code != 'NoSuchBucket' and (status == 404 or code in {'404', 'NoSuchKey', 'NotFound'}):
                # A missing bucket is not a missing object. Independently check
                # bucket availability before treating the target as absent.
                self.client.head_bucket(Bucket=BUCKET)
                raise FileNotFoundError(path) from error
            raise
        return result['Body'].read()

    def create(self, path, raw):
        self.check_key(path)
        if (not isinstance(raw, bytes) or not raw
                or hashlib.sha256(raw).hexdigest() != publication.PATTERN.fullmatch(path).group(2)):
            raise ValueError('audit S3 bytes must match the content-addressed key')
        result = self.client.put_object(Bucket=BUCKET, Key=path, Body=raw,
                                        ContentType='application/json', IfNoneMatch='*')
        meta = result.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise RuntimeError('audit S3 create response ambiguous')
