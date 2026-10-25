"""S3 inventory and exact batch deletion; Hub publication for control documents."""
try:
    from .reader_bucket_store import S3BucketStore, HubBucketStore
except ImportError:
    from reader_bucket_store import S3BucketStore, HubBucketStore


class CollectionStore(S3BucketStore):
    deletion_buckets = {'vomebook/reader-assets-v2', 'vomebook/pdf-pages-v2', 'melsm/pdf-archive-v2'}
    def __init__(self):
        super().__init__()
        self.writer = HubBucketStore()

    def put_bytes(self, bucket, path, raw):
        self.writer.put_bytes(bucket, path, raw)

    def put_json(self, bucket, path, payload):
        self.writer.put_json(bucket, path, payload)

    def delete(self, bucket, paths):
        if bucket not in self.deletion_buckets:
            raise ValueError('collection cannot delete outside its authorized bucket domain')
        if any(not path or path.startswith('/') or '\\' in path or '\0' in path
               or any(part in {'', '.', '..'} for part in path.split('/')) for path in paths):
            raise ValueError('invalid collection key')
        super().delete(bucket, paths)

    def stat(self, bucket, path):
        if (bucket, path) in self.object_sizes:
            return self.object_sizes[bucket, path]
        return self.writer.stat(bucket, path)


class CcrdCollectionStore(CollectionStore):
    """The CCRD publisher lock owns this separate deletion domain."""
    deletion_buckets = {'vomebook/ccrd-index'}
