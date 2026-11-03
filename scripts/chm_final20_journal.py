"""Deterministic append-only journal for exactly the last 20 CHM EPUBs.

Pure validation only. A caller must hold workflow locks, bind the prior
352-item plan, and distinguish a 404 from bucket loss. The user explicitly
requires removal even when a target remains referenced.
"""
import copy
import hashlib
import json
import re

try:
    from . import chm_subset_journal as model
except ImportError:
    import chm_subset_journal as model

TOKEN = re.compile(r'[0-9a-f]{32}')
SHA = re.compile(r'[0-9a-f]{64}')
ROWS_SHA = '5461e0c58d33b9ba60ba3bbff276867e6d95ebc6554023c17931883038ce8ce2'
KIND = 'chm-final20-cursor-v1'


def plan(rows, prior_plan, operator_root):
    if (not isinstance(rows, list) or not rows or not isinstance(prior_plan, str)
            or not SHA.fullmatch(prior_plan) or not isinstance(operator_root, str) or not SHA.fullmatch(operator_root)):
        raise ValueError('invalid final CHM plan inputs')
    paths = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'bucket', 'path', 'bytes', 'sha256'}
                or row['bucket'] != model.subset.ASSETS or not isinstance(row['path'], str)
                or not model.subset.TARGET.fullmatch(row['path']) or type(row['bytes']) is not int
                or row['bytes'] < 1 or not isinstance(row['sha256'], str) or not SHA.fullmatch(row['sha256'])):
            raise ValueError('invalid final CHM target identity')
        paths.append(row['path'])
    if paths != sorted(set(paths)):
        raise ValueError('final CHM targets are duplicated or unordered')
    return {'kind': 'chm-final20-plan-v1', 'prior_plan_sha256': prior_plan,
            'operator_root_sha256': operator_root, 'targets': copy.deepcopy(rows), 'deletion_authorized': False}


def fixed_plan(rows, prior_plan, operator_root):
    if (len(rows) != 20 or sum(row['bytes'] for row in rows) != 18427134
            or model.digest(rows) != ROWS_SHA):
        raise ValueError('last 20 target scope differs')
    return plan(rows, prior_plan, operator_root)


def envelope(bound, state, *, sequence, parent, nonce):
    if (not isinstance(bound, dict) or set(bound) != {'kind', 'prior_plan_sha256', 'operator_root_sha256',
                                                      'targets', 'deletion_authorized'}
            or bound != plan(bound.get('targets'), bound.get('prior_plan_sha256'), bound.get('operator_root_sha256'))):
        raise ValueError('invalid final CHM plan')
    if (not isinstance(state, dict) or set(state) != {'pending', 'receipt', 'root_pending', 'complete'}
            or type(state['complete']) is not bool or type(state['root_pending']) is not bool):
        raise ValueError('invalid final CHM cursor')
    paths = [row['path'] for row in bound['targets']]
    pending, receipt = state['pending'], state['receipt']
    if pending is not None and pending != paths:
        raise ValueError('final CHM intent is not exact')
    if receipt is not None and receipt != paths:
        raise ValueError('final CHM receipt is not exact')
    if (pending is not None and receipt is not None or state['root_pending'] and receipt != paths
            or state['complete'] and (pending is not None or receipt != paths or state['root_pending'])):
        raise ValueError('invalid final CHM state combination')
    if (type(sequence) is not int or sequence < 0 or not isinstance(nonce, str) or not TOKEN.fullmatch(nonce)
            or nonce == '0' * 32 or sequence == 0 and parent is not None
            or sequence > 0 and (not isinstance(parent, str) or not SHA.fullmatch(parent))):
        raise ValueError('invalid final CHM event identity')
    if sequence == 0 and (pending is not None or receipt is not None or state['root_pending'] or state['complete']):
        raise ValueError('final CHM initial event must be empty')
    return {'kind': KIND, 'plan_sha256': model.digest(bound), 'sequence': sequence,
            'parent_sha256': parent, 'nonce': nonce, 'state': copy.deepcopy(state),
            'original_complete': False, 'producer_restore_authorized': False, 'deletion_authorized': False}


def transition(bound, before, after):
    for event in (before, after):
        if (not isinstance(event, dict) or set(event) != {'kind', 'plan_sha256', 'sequence', 'parent_sha256',
                                                          'nonce', 'state', 'original_complete',
                                                          'producer_restore_authorized', 'deletion_authorized'}
                or event != envelope(bound, event['state'], sequence=event['sequence'],
                                     parent=event['parent_sha256'], nonce=event['nonce'])):
            raise ValueError('invalid final CHM event')
    if (after['sequence'] != before['sequence'] + 1 or after['parent_sha256'] != model.digest(before)
            or after['nonce'] == before['nonce']):
        raise ValueError('invalid final CHM event chain')
    old, new = before['state'], after['state']
    paths = [row['path'] for row in bound['targets']]
    allowed = ((old == {'pending': None, 'receipt': None, 'root_pending': False, 'complete': False}
                and new == {'pending': paths, 'receipt': None, 'root_pending': False, 'complete': False})
               or (old == {'pending': paths, 'receipt': None, 'root_pending': False, 'complete': False}
                   and new == {'pending': None, 'receipt': paths, 'root_pending': False, 'complete': False})
               or (old == {'pending': None, 'receipt': paths, 'root_pending': False, 'complete': False}
                   and new == {'pending': None, 'receipt': paths, 'root_pending': True, 'complete': False})
               or (old == {'pending': None, 'receipt': paths, 'root_pending': True, 'complete': False}
                   and new == {'pending': None, 'receipt': paths, 'root_pending': False, 'complete': True}))
    if not allowed:
        raise ValueError('invalid final CHM state transition')


def initial(bound):
    state = {'pending': None, 'receipt': None, 'root_pending': False, 'complete': False}
    nonce = hashlib.sha256(model.encode(bound) + b':initial').hexdigest()[:32]
    return envelope(bound, state, sequence=0, parent=None, nonce=nonce)


class Cursor:
    def __init__(self, backend, bound):
        self.backend, self.plan = backend, bound
        initial(bound)

    def _events(self):
        names = self.backend.names()
        if not isinstance(names, set) or 'plan.json' not in names:
            raise ValueError('final CHM journal namespace invalid')
        event_names = sorted(name for name in names if name.startswith('event-'))
        if (not event_names or names != {'plan.json', *event_names}
                or event_names != [f'event-{i:08d}.json' for i in range(len(event_names))]
                or len(event_names) > 5 or self.backend.read('plan.json') != model.encode(self.plan)):
            raise ValueError('final CHM journal sequence or plan differs')
        events, nonces = [], set()
        for seq, name in enumerate(event_names):
            raw = self.backend.read(name)
            try:
                event = json.loads(raw)
            except (ValueError, TypeError) as error:
                raise ValueError('invalid final CHM event JSON') from error
            if (not isinstance(event, dict) or model.encode(event) != raw or event.get('sequence') != seq
                    or event.get('nonce') in nonces):
                raise ValueError('invalid final CHM event bytes')
            if seq == 0 and event != initial(self.plan):
                raise ValueError('final CHM initial event differs')
            if events:
                transition(self.plan, events[-1], event)
            events.append(event)
            nonces.add(event['nonce'])
        return events

    def head(self):
        return self._events()[-1]

    def _create(self, name, raw):
        # Backend raises OutcomeUnknown on any ambiguous PUT. Never retry here.
        created = self.backend.create(name, raw)
        if created is not True or self.backend.read(name) != raw:
            raise RuntimeError('final CHM journal write is not verified')

    def initialize(self):
        names = self.backend.names()
        if names == set():
            self._create('plan.json', model.encode(self.plan))
            names = self.backend.names()
        if names == {'plan.json'}:
            if self.backend.read('plan.json') != model.encode(self.plan):
                raise ValueError('partial final CHM plan differs')
            self._create('event-00000000.json', model.encode(initial(self.plan)))
        return self.head()

    def append(self, before, after):
        events = self._events()
        if events[-1] != before:
            raise ValueError('final CHM journal head changed')
        transition(self.plan, before, after)
        if any(item['nonce'] == after['nonce'] for item in events):
            raise ValueError('final CHM nonce reused')
        self._create(f'event-{after["sequence"]:08d}.json', model.encode(after))
        if self.head() != after:
            raise ValueError('final CHM journal readback differs')
