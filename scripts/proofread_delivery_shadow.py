"""Synthetic, offline parse-delivery journal; NOT a production queue or writer.

The model covers two PRs, one tracker issue and tracker comments only. It has no
network, database, workflow, scheduler, claim expiry or activation entry point.
Crop uploads, PR comments, auto-merge, real authentication, receipt retention,
and the completeness of the external effect inventory are NOT modeled. Evidence
is a historical observation: the model does not re-read effects before another
event, prove freshness, inspect actual file contents, or validate user payloads.
All modeled effects are required and ordered; partial evidence is never a
complete receipt. Status names are synthetic and not a public API contract.

An event hash chain detects truncation and accidental changes only relative to
an independently retained head checkpoint; it does not authenticate a writer
or prove external effects. The checkpoint itself needs protected, durable CAS.
All evidence is caller-supplied and may be forged or stale. A synthetic claim
does not fence any real worker. Even a fully verified synthetic journal never
authorizes retry, production recovery, opening submissions or executing Actions.
"""

import copy
import hashlib
import hmac
import json
import re


HEX64 = re.compile(r'[0-9a-f]{64}\Z')
GIT_SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
HEAD_REF = re.compile(r'proofread/[a-z0-9][a-z0-9-]{0,95}\Z')
OWNER = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
PLAN_KEYS = {'version', 'scope', 'submission_id', 'payload_sha256', 'source_sha256',
             'run', 'config_pr', 'patch_pr', 'tracker', 'receipt_verifier'}
RUN_KEYS = {'repository_id', 'repository_sha', 'workflow_path', 'run_id', 'attempt'}
PR_KEYS = {'repository_id', 'base_ref', 'base_sha', 'head_ref', 'head_sha', 'files_sha256'}
TRACKER_KEYS = {'repository_id', 'body_sha256', 'comment_sha256'}
CLAIM_FIELDS = {'owner', 'epoch'}
MAX_TRACKER_COMMENTS = 128
MAX_EVENTS = 2048


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def receipt_verifier(capability):
    """Hash an injected fixture capability; this does not establish entropy."""
    if type(capability) is not bytes or len(capability) != 32:
        raise ValueError('synthetic receipt capability must be 32 bytes')
    return hashlib.sha256(b'parse-delivery-shadow-receipt-v1\0' + capability).hexdigest()


def _hex(value):
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _positive(value):
    return type(value) is int and value > 0


def _validate_plan(plan):
    if (not isinstance(plan, dict) or set(plan) != PLAN_KEYS or type(plan['version']) is not int
            or plan['version'] != 1 or plan['scope'] != 'synthetic-parse-pr-tracker-only'
            or not all(_hex(plan[key]) for key in ('submission_id', 'payload_sha256', 'source_sha256',
                                                   'receipt_verifier'))):
        raise ValueError('invalid synthetic delivery plan')
    run = plan['run']
    if (not isinstance(run, dict) or set(run) != RUN_KEYS or not _positive(run['repository_id'])
            or not isinstance(run['repository_sha'], str) or not GIT_SHA.fullmatch(run['repository_sha'])
            or run['workflow_path'] != '.github/workflows/proofread-submit-parse.yml'
            or not _positive(run['run_id']) or not _positive(run['attempt'])):
        raise ValueError('invalid synthetic workflow binding')
    for key, expected_base in (('config_pr', 'config'), ('patch_pr', 'ocr_patch')):
        pr = plan[key]
        if (not isinstance(pr, dict) or set(pr) != PR_KEYS or not _positive(pr['repository_id'])
                or pr['base_ref'] != expected_base or not isinstance(pr['base_sha'], str)
                or not GIT_SHA.fullmatch(pr['base_sha']) or not isinstance(pr['head_sha'], str)
                or not GIT_SHA.fullmatch(pr['head_sha']) or not isinstance(pr['head_ref'], str)
                or not HEAD_REF.fullmatch(pr['head_ref']) or not _hex(pr['files_sha256'])):
            raise ValueError('invalid synthetic PR binding')
    if (plan['config_pr']['repository_id'] != plan['patch_pr']['repository_id']
            or plan['config_pr']['head_ref'] == plan['patch_pr']['head_ref']):
        raise ValueError('inconsistent synthetic PR repositories or branches')
    tracker = plan['tracker']
    if (not isinstance(tracker, dict) or set(tracker) != TRACKER_KEYS
            or not _positive(tracker['repository_id']) or not _hex(tracker['body_sha256'])
            or not isinstance(tracker['comment_sha256'], list)
            or not 1 <= len(tracker['comment_sha256']) <= MAX_TRACKER_COMMENTS
            or any(not _hex(value) for value in tracker['comment_sha256'])
            or len(set(tracker['comment_sha256'])) != len(tracker['comment_sha256'])):
        raise ValueError('invalid synthetic tracker binding')


def _effects(plan):
    return ['config_pr', 'patch_pr', 'tracker_issue'] + [
        f'tracker_comment:{index}' for index in range(len(plan['tracker']['comment_sha256']))
    ]


def new_journal(plan):
    """Initialize data only. Returning a journal is not a durable acceptance."""
    _validate_plan(plan)
    plan = copy.deepcopy(plan)
    return {'version': 1, 'plan': plan, 'plan_sha256': _digest(plan), 'events': []}


def _initial(plan):
    return {'owner': None, 'epoch': None, 'effects': {name: 'waiting' for name in _effects(plan)},
            'objects': {}, 'active': None}


def _common_evidence(journal, effect, value):
    plan = journal['plan']
    common = {'submission_id': plan['submission_id'], 'payload_sha256': plan['payload_sha256'],
              'source_sha256': plan['source_sha256'], 'plan_sha256': journal['plan_sha256'], 'effect': effect}
    return isinstance(value, dict) and all(value.get(key) == item for key, item in common.items()), set(common)


def _validate_evidence(journal, state, effect, evidence):
    """Check synthetic bindings, not the truth, freshness or authorship of evidence."""
    matches, keys = _common_evidence(journal, effect, evidence)
    plan = journal['plan']
    if not matches:
        raise ValueError('synthetic evidence identity differs')
    if effect in ('config_pr', 'patch_pr'):
        binding = plan[effect]
        keys |= PR_KEYS | {'number', 'state', 'inventory_complete'}
        if (set(evidence) != keys or not _positive(evidence['repository_id'])
                or any(evidence.get(key) != value for key, value in binding.items())
                or not _positive(evidence['number']) or evidence['state'] != 'open'
                or evidence['inventory_complete'] is not True):
            raise ValueError('synthetic PR evidence differs')
        other = 'patch_pr' if effect == 'config_pr' else 'config_pr'
        if other in state['objects'] and evidence['number'] == state['objects'][other]:
            raise ValueError('synthetic PR numbers collide')
    elif effect == 'tracker_issue':
        keys |= {'repository_id', 'body_sha256', 'number', 'state', 'config_number', 'patch_number',
                 'inventory_complete'}
        if (set(evidence) != keys or not _positive(evidence['repository_id'])
                or evidence['repository_id'] != plan['tracker']['repository_id']
                or evidence['body_sha256'] != plan['tracker']['body_sha256']
                or not _positive(evidence['number']) or evidence['state'] != 'open'
                or not _positive(evidence['config_number']) or not _positive(evidence['patch_number'])
                or evidence['config_number'] != state['objects'].get('config_pr')
                or evidence['patch_number'] != state['objects'].get('patch_pr')
                or evidence['inventory_complete'] is not True):
            raise ValueError('synthetic tracker evidence differs')
    else:
        index = int(effect.partition(':')[2])
        keys |= {'repository_id', 'issue_number', 'number', 'body_sha256', 'inventory_complete'}
        if (set(evidence) != keys or not _positive(evidence['repository_id'])
                or evidence['repository_id'] != plan['tracker']['repository_id']
                or not _positive(evidence['issue_number'])
                or evidence['issue_number'] != state['objects'].get('tracker_issue')
                or evidence['body_sha256'] != plan['tracker']['comment_sha256'][index]
                or not _positive(evidence['number']) or evidence['inventory_complete'] is not True
                or evidence['number'] in [value for key, value in state['objects'].items()
                                          if key.startswith('tracker_comment:')]):
            raise ValueError('synthetic comment evidence differs')


def _apply(journal, state, event):
    kind = event.get('type')
    fields = set(event) - {'seq', 'previous_sha256', 'event_sha256'}
    if kind == 'claim':
        if (fields != {'type'} | CLAIM_FIELDS or state['owner'] is not None
                or not isinstance(event['owner'], str) or not OWNER.fullmatch(event['owner'])
                or not _positive(event['epoch'])):
            raise ValueError('invalid synthetic claim')
        state['owner'], state['epoch'] = event['owner'], event['epoch']
        return
    if not isinstance(kind, str) or kind not in {'begin', 'ack', 'timeout', 'observe_absent', 'verify', 'conflict'}:
        raise ValueError('unknown synthetic event')
    required = {'type', 'effect'} | CLAIM_FIELDS
    if kind == 'verify':
        required.add('evidence')
    if (fields != required or state['owner'] is None or event.get('owner') != state['owner']
            or type(event.get('epoch')) is not int or event['epoch'] != state['epoch']):
        raise ValueError('synthetic claim or event fields differ')
    effect = event.get('effect')
    if not isinstance(effect, str) or effect not in state['effects']:
        raise ValueError('unknown synthetic effect')
    current = state['effects'][effect]
    effects = _effects(journal['plan'])
    if kind == 'begin':
        if (current != 'waiting' or state['active'] is not None
                or any(state['effects'][name] != 'verified' for name in effects[:effects.index(effect)])):
            raise ValueError('synthetic effect cannot begin')
        state['effects'][effect], state['active'] = 'in_flight', effect
    elif state['active'] != effect:
        raise ValueError('synthetic effect is not active')
    elif kind in {'ack', 'timeout'}:
        if current != 'in_flight':
            raise ValueError('synthetic response is not pending')
        state['effects'][effect] = 'acknowledged' if kind == 'ack' else 'uncertain'
    elif kind == 'observe_absent':
        if current not in {'in_flight', 'acknowledged', 'uncertain'}:
            raise ValueError('synthetic absence cannot be observed')
        # A missing observation does not prove that a timed-out writer stopped.
        state['effects'][effect] = 'uncertain'
    elif kind == 'conflict':
        if current not in {'in_flight', 'acknowledged', 'uncertain'}:
            raise ValueError('synthetic conflict cannot be observed')
        state['effects'][effect] = 'blocked'
    else:
        if current not in {'in_flight', 'acknowledged', 'uncertain'}:
            raise ValueError('synthetic effect cannot be verified')
        _validate_evidence(journal, state, effect, event['evidence'])
        state['effects'][effect], state['active'] = 'verified', None
        state['objects'][effect] = event['evidence']['number']


def replay(journal, expected_plan_sha256, expected_revision, expected_last_sha256):
    """Replay against an independently retained plan and journal-head checkpoint."""
    if (not isinstance(journal, dict) or set(journal) != {'version', 'plan', 'plan_sha256', 'events'}
            or type(journal['version']) is not int or journal['version'] != 1
            or not isinstance(journal['events'], list) or len(journal['events']) > MAX_EVENTS):
        raise ValueError('invalid synthetic journal')
    _validate_plan(journal['plan'])
    if (not _hex(expected_plan_sha256) or journal['plan_sha256'] != expected_plan_sha256
            or _digest(journal['plan']) != expected_plan_sha256):
        raise ValueError('synthetic plan digest differs')
    if (type(expected_revision) is not int or expected_revision < 0 or not _hex(expected_last_sha256)
            or expected_revision != len(journal['events'])):
        raise ValueError('synthetic journal checkpoint differs')
    state = _initial(journal['plan'])
    previous = expected_plan_sha256
    for index, event in enumerate(journal['events'], 1):
        if (not isinstance(event, dict) or type(event.get('seq')) is not int or event['seq'] != index
                or event.get('previous_sha256') != previous or not _hex(event.get('event_sha256'))
                or _digest({key: value for key, value in event.items() if key != 'event_sha256'})
                != event['event_sha256']):
            raise ValueError('synthetic event chain differs')
        _apply(journal, state, event)
        previous = event['event_sha256']
    if previous != expected_last_sha256:
        raise ValueError('synthetic journal checkpoint differs')
    return {'revision': len(journal['events']), 'last_sha256': previous, **state,
            'production_recovery_authorized': False, 'production_retry_authorized': False,
            'submission_open_authorized': False, 'external_writers_fenced': False,
            'external_effects_proved': False}


def append(journal, expected_plan_sha256, expected_revision, expected_last_sha256, event):
    """Return a new synthetic journal, rejecting a stale caller's revision.

    The caller must provide real atomic persistence separately; this function's
    revision check cannot prevent two processes from committing separate forks.
    """
    state = replay(journal, expected_plan_sha256, expected_revision, expected_last_sha256)
    if state['revision'] >= MAX_EVENTS:
        raise ValueError('synthetic journal is full')
    if not isinstance(event, dict) or set(event) & {'seq', 'previous_sha256', 'event_sha256'}:
        raise ValueError('invalid synthetic event envelope')
    item = copy.deepcopy(event)
    item.update({'seq': state['revision'] + 1, 'previous_sha256': state['last_sha256']})
    item['event_sha256'] = _digest(item)
    _apply(journal, state, item)
    result = copy.deepcopy(journal)
    result['events'].append(item)
    return result


def classify_duplicate(journal, expected_plan_sha256, expected_revision, expected_last_sha256,
                       submission_id, payload_sha256, source_sha256):
    """Only compare identity; a match is not permission to dispatch or retry."""
    state = replay(journal, expected_plan_sha256, expected_revision, expected_last_sha256)
    plan = journal['plan']
    if (submission_id, payload_sha256, source_sha256) != (
            plan['submission_id'], plan['payload_sha256'], plan['source_sha256']):
        raise ValueError('synthetic submission identity conflict')
    return {'duplicate': True, 'revision': state['revision'], 'retry_authorized': False}


def receipt(journal, expected_plan_sha256, expected_revision, expected_last_sha256, capability):
    """Return minimal synthetic status, never submitted text or object details."""
    # This comparison is not a substitute for an authenticated production API,
    # secure random issuance, rate limits, durable storage or retention policy.
    state = replay(journal, expected_plan_sha256, expected_revision, expected_last_sha256)
    try:
        verifier = receipt_verifier(capability)
    except ValueError:
        verifier = ''
    if not hmac.compare_digest(verifier, journal['plan']['receipt_verifier']):
        raise LookupError('synthetic receipt unavailable')
    effects = list(state['effects'].values())
    if 'blocked' in effects:
        status = 'blocked'
    elif 'uncertain' in effects:
        status = 'uncertain'
    elif 'in_flight' in effects or 'acknowledged' in effects:
        status = 'verification_pending'
    elif all(value == 'verified' for value in effects):
        status = 'awaiting_review'
    elif 'verified' in effects:
        status = 'partial'
    elif state['owner'] is not None:
        status = 'claimed'
    else:
        status = 'queued'
    return {'status': status, 'revision': state['revision'], 'synthetic': True,
            'production_recovery_authorized': False}


def require_real_activation(_journal):
    raise RuntimeError('synthetic delivery model cannot authorize production activation')
