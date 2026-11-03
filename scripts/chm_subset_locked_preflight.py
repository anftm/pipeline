#!/usr/bin/env python3
"""Read-only preflight for the interrupted CHM transaction under Actions locks.

The caller is a reviewed nested workflow. This code cannot delete, publish a
retention root or journal, resume producers, or access a storage write method.
The three concurrency groups are held by Actions configuration, not attested by
this process. A clean result is a fresh point observation, not authorization.
"""
import hashlib
import gzip
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time

import httpx

try:
    from . import chm_epub_journal as journal, chm_epub_publication_preflight as chm
    from . import chm_epub_publication_safety as safety, chm_subset_retirement as subset
    from . import chm_subset_reconcile as reconcile
    from . import reader_gc_graph as graph, reader_gc_maintenance as maintenance
    from . import publish_reader_v3 as publication
    from .reader_bucket_store import S3BucketStore, retry_s3
    from .reader_gc_fence import GitHubProducerFence, producer_repositories
except ImportError:
    import chm_epub_journal as journal
    import chm_epub_publication_preflight as chm
    import chm_epub_publication_safety as safety
    import chm_subset_retirement as subset
    import chm_subset_reconcile as reconcile
    import reader_gc_graph as graph
    import reader_gc_maintenance as maintenance
    import publish_reader_v3 as publication
    from reader_bucket_store import S3BucketStore, retry_s3
    from reader_gc_fence import GitHubProducerFence, producer_repositories

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_REF = 'anftm/pipeline/.github/workflows/chm-subset-locked-preflight.yml@refs/heads/main'
PLAN_SHA = 'bc04c76ecf0977a65796d7eb8a982d778a191d7821a40077a07e8bb382bba3d4'
PROGRESS_SHA = '07aac595ddd43d3ee58bd8724d60ec5f74ecbea5a254eb683115d941bc14f312'
MAINTENANCE_SHA = '153c6c54402521ba4c6a5a897dd0c6ab4a85f9a9fd4ea6b3f920627b86d2c8b3'
RETENTION_SHA = '032fbb906fc60089c3e62681834535488fb0a4685a846d0c66381648c4b99399'
OWNER = '0507a077f1ee4c99b8c64c0de7b58035'
INVENTORY = ROOT / 'config/chm-history-inventory-20261030.json'
CLASSIFICATION = ROOT / 'config/chm-subset-classification-20261102.json'
CLASSIFICATION_SHA = 'aefc7f4a01ed6d75e0a493af67f2eca0fd404b83c28863ad80fdca9c5fda2b54'
RETENTION = 'reader-index/chm-epub-withdrawal-retention.json'
OUTPUT = ROOT / 'output/chm-subset-locked-preflight/result.json'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class ReadOnlyStore:
    """Expose only the reads required by the preflight, never any writer."""
    def __init__(self):
        self.reader = S3BucketStore()
        self.reader.read_cache = None

    def read_bytes(self, bucket, path):
        try:
            return self.reader.read_bytes(bucket, path)
        except FileNotFoundError:
            namespace, name = self.reader._location(bucket)
            retry_s3(lambda: self.reader._client(namespace).head_bucket(Bucket=name))
            raise

    def list_files(self, bucket, prefixes):
        return self.reader.list_files(bucket, prefixes)

    def assert_buckets_available(self):
        for bucket in graph.BUCKETS:
            namespace, name = self.reader._location(bucket)
            retry_s3(lambda: self.reader._client(namespace).head_bucket(Bucket=name))

    def assert_chm_bucket_available(self):
        namespace, name = self.reader._location(chm.ASSETS)
        retry_s3(lambda: self.reader._client(namespace).head_bucket(Bucket=name))
        return True


def github_get(endpoint):
    """GET-only producer observation; log repository and status, never secrets."""
    parts = endpoint.split('/')
    if len(parts) < 4 or parts[0] != 'repos':
        raise ValueError('invalid producer-fence GitHub endpoint')
    repo = '/'.join(parts[1:3])
    try:
        from .reader_gc_credentials import token_for
    except ImportError:
        from reader_gc_credentials import token_for
    token = token_for(repo)
    fallback = os.environ.get('GH_TOKEN', '')
    fallback_used = False
    url = 'https://api.github.com/' + endpoint
    pages = []
    while url:
        if not url.startswith('https://api.github.com/'):
            raise RuntimeError('unexpected GitHub pagination endpoint')
        for attempt in range(3):
            try:
                response = httpx.get(url, headers={'Authorization': 'Bearer ' + token,
                    'Accept': 'application/vnd.github+json', 'User-Agent': 'chm-subset-readonly-preflight'},
                    timeout=60, follow_redirects=False)
                status = response.status_code
                if status == 200:
                    break
                if status == 401 and fallback and token != fallback and not fallback_used and attempt < 2:
                    # A stored per-repository credential may have expired. Only
                    # retry this read with the separately configured read token;
                    # success still requires the full original fence checks.
                    token = fallback
                    fallback_used = True
                    continue
                if status not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                    raise RuntimeError('producer GitHub GET failed for ' + repo + ': HTTP ' + str(status))
            except httpx.TransportError as error:
                if attempt == 2:
                    raise RuntimeError('producer GitHub GET transport failed for ' + repo) from error
            time.sleep(2 ** attempt)
        pages.append(response.json())
        url = response.links.get('next', {}).get('url')
    return pages


def require_context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF
            or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('CHM preflight requires its central main workflow context')


def ensure_read_credentials():
    """Prefer the configured per-repository tokens; otherwise use one read token.

    The fallback never permits a missing/failed repository response: the same
    GitHub fence still checks every configured repository and active run.
    """
    if not os.environ.get('GC_PRODUCER_TOKENS_JSON'):
        token = os.environ.get('GH_TOKEN')
        if not token:
            raise RuntimeError('producer GitHub read credential unavailable')
        os.environ['GC_PRODUCER_TOKENS_JSON'] = json.dumps({repo: token for repo in producer_repositories()})


def _identity(store):
    progress_raw = store.read_bytes(chm.ASSETS, journal.PROGRESS)
    maintenance_raw = store.read_bytes(chm.ASSETS, maintenance.DURABLE_JOURNAL)
    retention_raw = store.read_bytes(chm.ASSETS, RETENTION)
    if (digest(progress_raw), digest(maintenance_raw), digest(retention_raw)) != (
            PROGRESS_SHA, MAINTENANCE_SHA, RETENTION_SHA):
        raise ValueError('original CHM progress, maintenance or retention bytes changed')
    progress, window = json.loads(progress_raw), json.loads(maintenance_raw)
    if (progress.get('plan_sha256') != PLAN_SHA or progress.get('maintenance_id') != OWNER
            or progress.get('complete') is not False or progress.get('confirmed_deleted_objects') != 600
            or window.get('maintenance_id') != OWNER or window.get('callback_plan_sha256') != PLAN_SHA
            or window.get('callback_recovery_required') is not True or window.get('restored') is True):
        raise ValueError('original CHM maintenance window or progress differs')
    return progress_raw, maintenance_raw, retention_raw, progress


def verify_selected_manifests(store, rows, selected, plan, progress, updates):
    """Read exactly the already mapped 358 manifest documents, never chapters."""
    histories = safety.load_plan_history_catalogs(store, plan)
    if len(histories) != 5:
        raise ValueError('original historical catalog inventory differs')
    historical_sidecars = {generation: publication.pinned_base(store, catalog, None)
                           for generation, catalog in histories.items()}
    current = json.loads(gzip.decompress(plan['overrides'][chm.SIDECAR]))
    index = json.loads(plan['overrides'][chm.INDEX])
    evidence = progress['chapter_evidence']['results'] + [item['chapter'] for item in updates.values()]
    evidence_by_path = {item['manifest']: item['manifest_sha256'] for item in evidence}
    if len(evidence) != 950 or len(evidence_by_path) != 950:
        raise ValueError('original chapter manifest evidence differs')
    checklist = subset.verify_route_limit_inputs(rows, selected, historical_sidecars,
                                                 current, index, evidence_by_path)['manifests']
    if len(checklist) != 358:
        raise ValueError('selected CHM chapter manifest count differs')
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda path: (path, store.read_bytes(chm.ASSETS, path)), sorted(checklist)))
    return subset.verify_manifest_bytes(checklist, dict(values))


def check(store, fence, client, rows, classification_raw):
    if digest(classification_raw) != CLASSIFICATION_SHA:
        raise ValueError('saved subset classification SHA differs')
    classification = json.loads(classification_raw)
    if (classification.get('old_plan_sha256') != PLAN_SHA or classification.get('maintenance_id') != OWNER
            or classification.get('deletion_authorized') is not False):
        raise ValueError('saved subset classification identity differs')
    indexed = {row['path']: row for row in rows}
    selected = sorted((indexed[item['path']] for item in classification['targets']
                       if item['category'] == 'mapped-with-saved-evidence'), key=lambda item: item['path'])
    withheld = sorted((indexed[item['path']] for item in classification['targets']
                       if item['category'] in {'needs-independent-review', 'withheld-oversize'}),
                      key=lambda item: item['path'])
    progress_raw, maintenance_raw, retention_raw, progress = _identity(store)
    partition = subset.verify_exact_partition(rows, progress['batches'], selected, withheld)
    if (partition['old_receipts'], partition['selected'], partition['withheld']) != (600, 352, 20):
        raise ValueError('saved subset partition is not exact')
    plan, _, updates, plan_sha = journal.load_plan(store, rows)
    if plan_sha != PLAN_SHA or plan['overrides'][RETENTION] != retention_raw:
        raise ValueError('original plan or retention root differs')
    if len(plan.get('rollback_aliases', {})) != 5:
        raise ValueError('original plan must contain all five rollback aliases')
    journal.verify_initial_chapter_evidence(plan, updates, progress)
    before = fence.check()
    safety.verify_live_chm_controls(store, plan)
    safety.verify_live_processing_controls(store, updates)
    safety.verify_retired_rollback_aliases(store, plan)
    sites = safety.observe_planned_projection(store, client, plan)
    manifest_validation = verify_selected_manifests(store, rows, selected, plan, progress, updates)
    # A fresh GET of every exact target binds the old 600 receipts and checks
    # the complete bytes of the 352 candidates plus 20 retained objects. Bucket
    # availability is independently checked for each missing object by the
    # read-only adapter; this does not claim an atomic snapshot.
    def read_exact(bucket, path):
        try:
            return store.read_bytes(bucket, path)
        except FileNotFoundError:
            return None
    cursor = {'receipts': [], 'pending': None, 'subset_complete': False,
              'original_complete': False, 'producer_restore_authorized': False}
    target_observations = reconcile.reconcile(rows, progress['batches'], selected, withheld, cursor,
                                               read_exact, workers=4)
    expected_counts = {'old-verified-absent': 600, 'unsubmitted-present': 352, 'withheld-present': 20}
    if {key: value['objects'] for key, value in target_observations['counts'].items()} != expected_counts:
        raise ValueError('exact 600/352/20 target observations differ')
    retained = []
    for row in withheld:
        raw = store.read_bytes(row['bucket'], row['path'])
        if len(raw) != row['bytes'] or digest(raw) != row['sha256']:
            raise ValueError('withheld EPUB changed: ' + row['path'])
        root = row['path'].rsplit('/', 1)[0] + '/'
        siblings = store.list_files(row['bucket'], (root,))
        if siblings != {row['path']}:
            raise ValueError('withheld EPUB parent has changed siblings: ' + root)
        retained.append({'path': row['path'], 'bytes': len(raw), 'sha256': digest(raw)})
    after_sites = safety.observe_planned_projection(store, client, plan)
    for site in ('hf', 'pages'):
        for field in ('surface', 'generation', 'catalog_sha256'):
            if sites[site].get(field) != after_sites[site].get(field):
                raise ValueError('Reader projection changed during preflight: ' + site)
    safety.verify_live_chm_controls(store, plan)
    safety.verify_live_processing_controls(store, updates)
    safety.verify_retired_rollback_aliases(store, plan)
    after = fence.check()
    store.assert_buckets_available()
    end = _identity(store)
    if end[:3] != (progress_raw, maintenance_raw, retention_raw):
        raise ValueError('original CHM controls changed during preflight')
    return {'kind': 'read-only-locked-chm-subset-preflight', 'partition': partition,
            'progress_sha256': digest(progress_raw), 'maintenance_sha256': digest(maintenance_raw),
            'retention_sha256': digest(retention_raw), 'withheld': retained,
            'target_observations': target_observations,
            'selected_manifest_limits': manifest_validation,
            'fence_before': before, 'fence_after': after,
            'sites_before': sites, 'sites_after': after_sites,
            'reader_acceptance_complete': True, 'rollback_alias_count': len(plan['rollback_aliases']),
            'root_created': False, 'journal_created': False, 'remote_mutations': 0,
            'deletion_authorized': False, 'producer_restore_authorized': False,
            'caveats': ['point observation; no actual four-bucket closure was rebuilt',
                        'Actions isolation groups do not fence other Hub or S3 writers',
                        'this program cannot write, delete, restore or authorize production mutation']}


def main():
    require_context()
    ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('CHM preflight output already exists')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rows = chm.preview.load_inventory(INVENTORY)['targets']
    store, fence = ReadOnlyStore(), GitHubProducerFence(api=github_get)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        result = check(store, fence, client, rows, CLASSIFICATION.read_bytes())
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps({'output': str(OUTPUT), 'sha256': digest(OUTPUT.read_bytes()),
                      'withheld_checked': len(result['withheld']), 'deletion_authorized': False}))


if __name__ == '__main__':
    main()
