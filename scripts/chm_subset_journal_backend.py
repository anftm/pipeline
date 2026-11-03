"""Storage-neutral append-only subset journal experiment; no production adapter.

This module takes an explicitly supplied backend, has no network client, and
never creates a retention root, deletes objects, resumes a producer or grants
authorization. Its conditional-create protocol relies on the backend's
correctness and cooperative writers. It does not establish a distributed lock,
authenticate writers or prove that a GET reflects durable storage.
"""
import json

try:
    from . import chm_subset_journal as model
except ImportError:
    import chm_subset_journal as model


class Conflict(RuntimeError):
    """The exact event key is occupied by different or malformed bytes."""


class OutcomeUnknown(RuntimeError):
    """A create request may have succeeded; do not blindly retry a mutation."""


class Cursor:
    """Immutable numbered event keys under one caller-owned test namespace.

    Backend interface:
      - ``read(name)`` -> bytes or None; errors (including access errors) raise.
      - ``names()`` -> exact set of names in the namespace, or raises.
      - ``create(name, body)`` -> True on creation, False on precondition failure;
        an ambiguous response raises OutcomeUnknown.

    No byte, event, or status returned by this class is production authority.
    A missing object may not be treated as an absent target; the backend must
    independently distinguish bucket errors from missing keys.
    """
    def __init__(self, backend, plan):
        if any(not callable(getattr(backend, name, None)) for name in ('read', 'names', 'create')):
            raise ValueError('journal backend interface is incomplete')
        initial = {'receipts': [], 'pending': None, 'subset_complete': False,
                   'original_complete': False, 'producer_restore_authorized': False}
        model.make_envelope(plan, initial, sequence=0, parent_sha256=None, nonce='1' * 32)
        self.backend, self.plan = backend, plan

    def _create_once(self, name, raw):
        """Normalize an unrecognized mutation response to unresolved.

        This cannot repair a backend that reports two successful conditional
        creates. It merely prevents a caller from treating a transport or
        adapter exception as a definite no-write result.
        """
        try:
            return self.backend.create(name, raw)
        except OutcomeUnknown:
            raise
        except Exception as error:
            raise OutcomeUnknown('backend create may have written; do not retry') from error

    @staticmethod
    def _event_name(sequence):
        if type(sequence) is not int or not 0 <= sequence <= 99_999_999:
            raise ValueError('journal sequence is out of range')
        return f'event-{sequence:08d}.json'

    def _events(self):
        names = self.backend.names()
        if (not isinstance(names, set) or any(not isinstance(name, str) for name in names)
                or 'plan.json' not in names):
            raise ValueError('journal namespace listing is invalid or incomplete')
        event_names = sorted(name for name in names if name.startswith('event-'))
        if (names != {'plan.json', *event_names} or not event_names
                or event_names != [self._event_name(i) for i in range(len(event_names))]):
            raise ValueError('journal namespace contains unknown files or a sequence gap')
        if self.backend.read('plan.json') != model.encode(self.plan):
            raise ValueError('journal plan bytes differ')
        events, nonces = [], set()
        for i, name in enumerate(event_names):
            raw = self.backend.read(name)
            try:
                item = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise ValueError('journal event is not readable JSON') from error
            if (not isinstance(item, dict) or set(item) != {
                    'kind', 'plan_sha256', 'sequence', 'parent_sha256', 'nonce', 'state',
                    'remote_mutations', 'deletion_authorized'} or model.encode(item) != raw
                    or item['sequence'] != i):
                raise ValueError('journal event bytes or sequence differ')
            expected = model.make_envelope(self.plan, item['state'], sequence=i,
                                           parent_sha256=item['parent_sha256'], nonce=item['nonce'])
            if item != expected or item['nonce'] in nonces:
                raise ValueError('journal event identity or nonce differs')
            if events:
                model.verify_transition(self.plan, events[-1], item)
            events.append(item)
            nonces.add(item['nonce'])
        return events

    def head(self):
        return self._events()[-1]

    def initialize(self, *, nonce):
        """Create plan and first event only in an empty namespace; never overwrite."""
        initial = {'receipts': [], 'pending': None, 'subset_complete': False,
                   'original_complete': False, 'producer_restore_authorized': False}
        first = model.make_envelope(self.plan, initial, sequence=0, parent_sha256=None, nonce=nonce)
        if self.backend.names() != set():
            raise Conflict('journal namespace is not empty')
        if not self._create_and_read('plan.json', model.encode(self.plan)):
            raise Conflict('journal plan appeared during initialization')
        if not self._create_and_read(self._event_name(0), model.encode(first)):
            raise Conflict('journal initial event appeared during initialization')
        if self.head() != first:
            raise Conflict('initial journal readback differs')
        return {'status': 'initialized-by-backend', 'deletion_authorized': False}

    def _create_and_read(self, name, raw):
        created = self._create_once(name, raw)
        if type(created) is not bool:
            raise OutcomeUnknown('backend did not give a definite create outcome')
        try:
            current = self.backend.read(name)
        except Exception as error:
            raise OutcomeUnknown('backend create readback failed; do not retry') from error
        if current != raw:
            raise Conflict('journal event differs after conditional create')
        return created

    def append(self, expected_head_sha256, after):
        events = self._events()
        head = events[-1]
        if model.digest(head) != expected_head_sha256:
            # A prior call can recover only if the exact next event is present;
            # this is consistency, not proof of request authorship.
            if (head == after and len(events) > 1 and model.digest(events[-2]) == expected_head_sha256):
                return {'status': 'already-present', 'deletion_authorized': False}
            raise Conflict('journal head changed')
        model.verify_transition(self.plan, head, after)
        if any(item['nonce'] == after['nonce'] for item in events):
            raise ValueError('journal nonce was reused')
        created = self._create_and_read(self._event_name(after['sequence']), model.encode(after))
        if self.head() != after:
            raise Conflict('journal changed after event creation')
        return {'status': 'created' if created else 'already-present', 'deletion_authorized': False}

    def reconcile_initialization(self, *, nonce):
        """Describe observed initial keys after interruption; never create keys.

        In particular, a plan-only observation is not permission to create the
        missing first event. The caller must separately resolve ownership and
        partial initialization before doing anything else.
        """
        initial = {'receipts': [], 'pending': None, 'subset_complete': False,
                   'original_complete': False, 'producer_restore_authorized': False}
        expected = model.make_envelope(self.plan, initial, sequence=0, parent_sha256=None, nonce=nonce)
        names = self.backend.names()
        if not isinstance(names, set):
            raise ValueError('journal namespace listing is invalid')
        if names == set():
            status = 'initialization-not-observed'
        elif names == {'plan.json'}:
            if self.backend.read('plan.json') != model.encode(self.plan):
                raise Conflict('partial initialization contains a foreign plan')
            status = 'plan-only-unresolved'
        elif names == {'plan.json', self._event_name(0)}:
            if self.head() != expected:
                raise Conflict('initialization contains a foreign initial event')
            status = 'initial-objects-observed'
        else:
            raise Conflict('initialization contains unexpected objects')
        return {'status': status, 'remote_mutations': 0, 'deletion_authorized': False,
                'caveat': 'readback does not prove authorship, durable ownership, or permit initialization retry'}
