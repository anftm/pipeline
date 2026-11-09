"""Repeat real read-only source, two-site Reader and index checks before staging.

This adapter wraps the existing exact plan inspector. It never accepts caller
booleans as evidence, does not write, and cannot isolate external writers.
The protected staging coordinator has no production CLI/workflow wiring.
"""
from __future__ import annotations

import hashlib

try:
    from . import reader_epub_plan as planning, reader_epub_shadow as shadow, search_snapshot
    from . import reader_format_backlog as backlog
except ImportError:
    import reader_epub_plan as planning
    import reader_epub_shadow as shadow
    import search_snapshot
    import reader_format_backlog as backlog


class LivePlanRevalidator:
    def __init__(self, *, hub, api, fs, client):
        self.hub, self.api, self.fs, self.client = hub, api, fs, client

    @staticmethod
    def _check_preview(plan, preview):
        shadow._verify_plan(plan)
        if (not isinstance(preview, dict) or preview.get('kind') != 'reader-epub-immutable-staging-preview'
                or preview.get('operation') != plan['operation'] or preview.get('plan_sha256') != plan['plan_sha256']
                or preview.get('search_generation') != plan['search_generation']
                or preview.get('reader_generation') != plan['reader_generation']
                or preview.get('remote_writes') != 0 or preview.get('index_written') is not False
                or preview.get('publication_ready') is not False
                or preview.get('external_writers_verified') is not False):
            raise ValueError('EPUB preview is not the exact unpromoted plan')
        digest = hashlib.sha256(search_snapshot.canonical({k: v for k, v in preview.items() if k != 'staging_sha256'})).hexdigest()
        if preview.get('staging_sha256') != digest:
            raise ValueError('EPUB preview digest differs')

    def __call__(self, plan, preview):
        self._check_preview(plan, preview)
        inspect = planning.inspect_one if plan['max_attempts'] == 1 else planning.inspect
        current = inspect(hub=self.hub, api=self.api, fs=self.fs, client=self.client,
                          shard=plan['shard'], operation=plan['operation'])
        if current != plan:
            raise ValueError('EPUB source, consumers or checkpoint changed before staging')
        return None

    def after_write(self, plan, preview, candidate):
        """Read the pinned sources and both consumers with the candidate shard in place."""
        self._check_preview(plan,preview)
        raw_pointer, manifest_raw, corpus, revisions, _ = backlog.capture_search(self.hub)
        pointer=backlog.search_publication_pointer.decode_pointer(raw_pointer)
        if (pointer['desired']!=plan['search_generation']
                or hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()!=plan['source_revisions_sha256']):
            raise ValueError('search sources changed during EPUB index observation')
        reader_pointer,catalog=backlog.publication.current(self.hub)
        if reader_pointer is None or reader_pointer['generation']!=plan['reader_generation'] or not catalog.get('base_sidecar'):
            raise ValueError('Reader generation changed during EPUB index observation')
        base=backlog.publication.pinned_base(self.hub,catalog,{})
        routes=backlog.publication.project_sidecar(base,catalog)['f']
        canonical_path='chapters/ebook/epub/index.json'
        shard_path=f"chapters/ebook/epub/index-{plan['shard']:02d}.json"
        canonical=backlog.matrix.read_json(self.fs,backlog.READER_BUCKET,canonical_path)
        shard=backlog.matrix.read_json(self.fs,backlog.READER_BUCKET,shard_path)
        if (hashlib.sha256(search_snapshot.canonical(canonical)).hexdigest()!=plan['canonical_index_sha256']
                or shard!=candidate or candidate.get('checkpoint_operation')!=plan['operation']):
            raise ValueError('EPUB canonical or candidate shard differs after write')
        old={row['key']:row for row in canonical.get('files',[])}
        changed={row['key']:row for row in candidate.get('files',[])}
        actions={row['key']:row for row in preview['actions']}
        requests=set()
        for source in plan['sources']:
            row=changed.get(source['key']);action=actions.get(source['key']);previous=old.get(source['key'])
            if (not action or not row or row.get('manifest')!=action['root']+'/chapter-manifest.json'
                    or row.get('source_sha256')!=action['source_sha256']
                    or row.get('source_revision')!=source['source_revision']):
                raise ValueError('EPUB candidate row differs from staged content')
            if (previous is None)!=(source['previous_manifest'] is None):
                raise ValueError('EPUB canonical source changed after shard write')
            if previous is not None:
                if (previous.get('manifest')!=source['previous_manifest']
                        or previous.get('source_revision')!=source['previous_revision']
                        or previous.get('source_sha256')!=source['previous_source_sha256']
                        or not backlog._matches_route(previous,routes.get(source['key']))):
                    raise ValueError('EPUB old route or source changed after shard write')
            elif source['key'] in routes:
                raise ValueError('new EPUB acquired a Reader route during staging')
            requests.add((source['repo'],source['source_revision'],source['path']))
            if source.get('proof')=='exact-revision-git-blobs':
                requests.add((source['repo'],source['previous_revision'],source['path']))
        metadata=backlog._metadata(self.api,requests)
        for source in plan['sources']:
            action=actions[source['key']]
            current=metadata.get((source['repo'],source['source_revision'],source['path']))
            if current is None:raise ValueError('EPUB current source metadata is unavailable')
            lfs=backlog._lfs_identity(current);blob=backlog._blob_identity(current)
            if source.get('reason')=='content_changed' and source.get('proof')=='current-lfs-sha256':
                if lfs!=source['current_source_sha256'] or lfs!=action['source_sha256']:
                    raise ValueError('EPUB current LFS content changed')
            elif source.get('reason')=='content_changed' and source.get('proof')=='exact-revision-git-blobs':
                previous=metadata.get((source['repo'],source['previous_revision'],source['path']))
                if (lfs is not None or backlog._lfs_identity(previous) is not None
                        or blob!=source['current_git_blob'] or backlog._blob_identity(previous)!=source['previous_git_blob']):
                    raise ValueError('EPUB old or current Git blob changed')
            elif source.get('reason')=='not_mapped':
                if (source.get('proof')!='unmapped-lfs-sha256' or lfs is None
                        or lfs!=source.get('current_source_sha256') or lfs!=action['source_sha256']):
                    raise ValueError('unmapped EPUB source lacks verifiable LFS content')
            else:raise ValueError('EPUB source proof is invalid')
        for repo,expected in sorted(revisions.items()):
            info=backlog.retry_read(lambda: self.api.repo_info(repo,repo_type='dataset',timeout=30))
            if getattr(info,'sha',None)!=expected:raise ValueError('dataset head changed after EPUB index write')
        for surface in ('pages','hf'):
            receipt=backlog.publication.observe_consumer(self.hub,surface,self.client)
            if receipt.get('generation')!=reader_pointer['generation'] or receipt.get('projection_verified') is not True:
                raise ValueError('Reader consumer changed after EPUB index write')
        if (self.hub.read_bytes(backlog.SEARCH_BUCKET,backlog.CURRENT)!=raw_pointer
                or self.hub.read_bytes(backlog.SEARCH_BUCKET,f"generations/{pointer['desired']}/manifest.json")!=manifest_raw
                or backlog.publication.current(self.hub)[0]!=reader_pointer
                or backlog.matrix.read_json(self.fs,backlog.READER_BUCKET,canonical_path)!=canonical
                or backlog.matrix.read_json(self.fs,backlog.READER_BUCKET,shard_path)!=candidate):
            raise ValueError('search or Reader roots changed during EPUB observation')
        return None
