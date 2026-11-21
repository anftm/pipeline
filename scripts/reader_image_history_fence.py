"""Read-only verification of an operator-established window for the reviewed h.PNG.

Keep the one central publisher run/attempt active and all other in-scope
workflows paused and drained for the entire operation, including recovery and
any consumer observation using this guard. Noncentral repositories must also
have Actions disabled. Keep external bucket writers/GC and out-of-band
restoration stopped throughout; the caller/operator owns continuity and ABA
prevention. Neither receipt nor repeated GitHub observations prove that.
The full GitHub check includes unrelated workflows (e.g. BHA) and the
prepared additional repositories. It needs readable Actions inventories and
policy for those repositories; inaccessible or deleted is not paused/drained.

After assigning the owner, retain an operator receipt in the Reader assets
bucket at RECEIPT_PREFIX + SHA256(exact JSON bytes) + '.json'. Supply that
digest independently to the publisher, not from the object being verified.
The v1 JSON schema is exactly: version (integer 1), kind (RECEIPT_KIND), key
(HPNG_KEY), operation (32 lowercase hex), reviewed_sha256 (64 lowercase hex),
workflow_ref (WORKFLOW_REF), head_sha (the full 40-hex main commit), and
owner_run_id / owner_run_attempt (positive decimal strings). Retain the exact
receipt bytes with the review; the separate external-writer declaration is
not a receipt field. Use a fresh, uncached read_bytes(bucket, path) adapter.

Pass WindowGuard(...) directly as runtime.guard (guard(phase)) or to v3
promotion (guard()). This does not acquire, release, or recover a fence.
reader-maintenance.yml cannot open this window: its pause disables this very
publisher, and the maintenance run is itself not exempt from all_workflows.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .publish_reader_v3 import ASSETS, PROTOCOL
from .reader_gc_fence import COLLECTOR, GitHubProducerFence, assert_workflow_context
from .reader_producer_scope_transition import unique_object

CENTRAL = 'anftm/pipeline'
WORKFLOW = '.github/workflows/reader-v3-publish.yml'
WORKFLOW_REF = f'{CENTRAL}/{WORKFLOW}@refs/heads/main'
WORKFLOW_NAME = 'Reader/桶 · Publish v3 Reading Generation'
HPNG_KEY = 'VoiceOfML/MLMRL-Hub\0马克思主义哲学吧精品贴/对实践唯物主义的研究/驳实践唯物主义/h.PNG'
RECEIPT_KIND = 'reader-image-history-window'
RECEIPT_PREFIX = 'reader-index/gc/image-history-fence/v1/'
MAX_RECEIPT = 4096

# These central workflows must be present, not merely absent from the active
# inventory. The shared fence checks all other returned workflows and runs too.
REQUIRED_PAUSED = {COLLECTOR} | {f'.github/workflows/{name}.yml' for name in (
    'reader-maintenance', 'reader-gc', 'reader-gc-ccrd', 'search-bucket-gc',
    'reader-image-stream', 'reader-format-daily',
)}


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(rf'[0-9a-f]{{{length}}}', value)


def _owner():
    assert_workflow_context(WORKFLOW)
    env = os.environ
    head = env.get('GITHUB_SHA')
    run_id, attempt = (env.get(name, '') for name in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'))
    if (env.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF or env.get('GITHUB_WORKFLOW') != WORKFLOW_NAME
            or env.get('GITHUB_REF') != 'refs/heads/main' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('READER_V3_WRITE_PROTOCOL') != PROTOCOL or not _hex(head, 40)
            or env.get('GITHUB_WORKFLOW_SHA') != head
            or not re.fullmatch(r'[1-9][0-9]*', run_id) or not re.fullmatch(r'[1-9][0-9]*', attempt)):
        raise RuntimeError('image history requires the exact central v3 publisher context and run attempt')
    return run_id, attempt, head


def _active_owner(fence, run_id, attempt, head):
    run = fence.single(f'repos/{CENTRAL}/actions/runs/{run_id}')
    if (type(run.get('id')) is not int or str(run['id']) != run_id
            or type(run.get('run_attempt')) is not int or str(run['run_attempt']) != attempt
            or run.get('path') not in (WORKFLOW, WORKFLOW + '@refs/heads/main')
            or run.get('status') != 'in_progress' or run.get('head_branch') != 'main'
            or run.get('event') != 'workflow_dispatch' or run.get('head_sha') != head):
        raise RuntimeError('exact image history publisher attempt is not active')


class WindowGuard:
    """Return True only after rechecking this exact window; raise on any gap.

    external_writers_stopped must be a separate, explicit operator confirmation
    (as for page migration), never derived from GitHub or from the receipt. A
    successful call is not publication/deletion authorization or verification of
    external credentials. A new owner or review requires a new pinned receipt.
    """

    def __init__(self, store, *, key, operation, reviewed_sha256, receipt_sha256,
                 external_writers_stopped, api=None):
        if key != HPNG_KEY or not _hex(operation, 32) or not _hex(reviewed_sha256, 64):
            raise ValueError('exact h.PNG operation and reviewed digest are required')
        if not _hex(receipt_sha256, 64):
            raise ValueError('independently pinned window receipt SHA-256 is required')
        if external_writers_stopped is not True:
            raise RuntimeError('separate confirmation that external writers are stopped is required')
        self._read = store.read_bytes
        self._key, self._operation, self._reviewed_sha256 = key, operation, reviewed_sha256
        self._receipt_sha256 = receipt_sha256
        self._fence = GitHubProducerFence(api)

    def _read_receipt(self):
        try:
            raw = self._read(ASSETS, RECEIPT_PREFIX + self._receipt_sha256 + '.json')
        except FileNotFoundError as error:
            raise RuntimeError('durable image history window receipt is missing') from error
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RECEIPT
                or hashlib.sha256(raw).hexdigest() != self._receipt_sha256):
            raise RuntimeError('durable image history window receipt differs from its pin')
        return raw

    def __call__(self, phase=None):
        """Recheck from scratch; the optional runtime phase does not change scope."""
        owner = _owner()
        run_id, attempt, head = owner
        raw = self._read_receipt()
        expected = {'version': 1, 'kind': RECEIPT_KIND, 'key': self._key,
                    'operation': self._operation, 'reviewed_sha256': self._reviewed_sha256,
                    'workflow_ref': WORKFLOW_REF, 'head_sha': head,
                    'owner_run_id': run_id, 'owner_run_attempt': attempt}
        try:
            receipt = json.loads(raw, object_pairs_hook=unique_object)
        except ValueError as error:
            raise RuntimeError('invalid durable image history window receipt') from error
        if receipt != expected or type(receipt['version']) is not int:
            raise RuntimeError('durable image history window receipt does not bind this owner and review')

        _active_owner(self._fence, *owner)
        report = self._fence.check(allow_current_workflow=WORKFLOW, all_workflows=True)
        disabled = next((row['disabled_workflows'] for row in report['repositories']
                         if row['repository'] == CENTRAL), ())
        if not REQUIRED_PAUSED.issubset(disabled):
            raise RuntimeError('known Reader producer/GC workflows missing from paused inventory')
        _active_owner(self._fence, *owner)
        if self._read_receipt() != raw or _owner() != owner:
            raise RuntimeError('image history window changed during verification')
        return True
