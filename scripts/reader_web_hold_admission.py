"""Read-only admission for separately approved v1 managed HTML hold batches.

Each registry is one exact scope, not an attestation that all holds are cleared.
The caller supplies a separately approved raw registry SHA, already validated
managed ZIP metadata and, for a positive proof, fresh trusted observations.
This module neither fetches evidence nor grants write authority. In particular,
source membership, hosted deployments, replay provenance, and writer/CAS checks
remain with the caller. No failure rows are removed or rewritten here.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import re

from . import reader_web_exact_plan as web

MEMBER = 'hold_admission.json'
DIGEST_FIELD = 'hold_admission_sha256'
MAX_REGISTRY = 128 * 1024
EXTENSION = 'html'
PROFILE = 'web-utf8-typography-v4'
MAX_DOCUMENT = 8 * 1024**2
MAX_CONTENT = 12 * 1024**2
WIDTH = 390
MAX_SCROLL = WIDTH + 1

_SCOPES = {'replay_holds': (21, 'reader_replay_mismatch'), 'output_budget': (1, 'output_budget')}
_NEW_PROOF = object()
REGISTRY_FIELDS = {'version', 'scope', 'search_generation', 'pages_commit', 'space_commit', 'reader_parent', 'rows'}
HOLD_FIELDS = {'key', 'repo', 'path', 'extension', 'source_revision', 'source_sha256',
               'profile', 'error', 'profile_upgrade_hold'}
CANDIDATE_FIELDS = {'source_revision', 'source_sha256', 'sha256', 'bytes'}
PARENT_FIELDS = {'generation', 'catalog_sha256'}
ARCHIVE_FACTS = {'mismatch', 'images', 'decoded', 'width', 'scroll', 'blocked_requests', 'passed'}
CLEAN_FACTS = {'posts', 'rendered', 'exact', 'images', 'decoded', 'width', 'scroll'}


class Registry(Mapping):
    """Authenticated registry, with JSON-shaped copies for caller inspection."""

    def __init__(self, raw, expected_sha256):
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REGISTRY
                or not web._is_sha(expected_sha256) or _digest(raw) != expected_sha256):
            raise ValueError('hold admission registry digest or size differs')
        self.__value = _registry(raw)
        self.__sha256 = expected_sha256

    @property
    def sha256(self):
        return self.__sha256

    def __getitem__(self, key):
        return deepcopy(self.__value[key])

    def __iter__(self):
        return iter(self.__value)

    def __len__(self):
        return len(self.__value)


def _digest(raw):
    return sha256(raw).hexdigest()


def _seal(value):
    return _digest(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii'))


def _matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _commit(value):
    return _matches(value, r'[0-9a-f]{40}')


def _fields(value, names):
    return isinstance(value, dict) and value.keys() == names


def _strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate member')
            result[key] = value
        return result

    def no_float(_):
        raise ValueError('non-integer number')

    def bounded_int(token):
        if len(token) > 12:
            raise ValueError('integer exceeds limit')
        return int(token)

    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_int=bounded_int,
                          parse_float=no_float, parse_constant=no_float)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('invalid hold admission registry JSON') from None


def _registry(raw):
    value = _strict_json(raw)
    if (not _fields(value, REGISTRY_FIELDS) or type(value['version']) is not int or value['version'] != 1
            or not web._is_sha(value['search_generation']) or not _commit(value['pages_commit'])
            or not _commit(value['space_commit']) or not _fields(value['reader_parent'], PARENT_FIELDS)
            or not all(web._is_sha(v) for v in value['reader_parent'].values())):
        raise ValueError('invalid v1 hold admission registry envelope')
    scope, rows = value['scope'], value['rows']
    if not isinstance(scope, str) or scope not in _SCOPES:
        raise ValueError('invalid v1 hold admission scope')
    count, reason = _SCOPES[scope]
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError(f'hold admission scope {scope} requires exactly {count} rows')
    keys, hashes = set(), set()
    total = 0
    for row in rows:
        if (not _fields(row, {'failure', 'old_source_sha256', 'candidate', 'require_clean'})
                or not web._is_sha(row['old_source_sha256']) or type(row['require_clean']) is not bool):
            raise ValueError('invalid hold admission registry row')
        hold, candidate = row['failure'], row['candidate']
        if (not _fields(hold, HOLD_FIELDS) or hold['profile_upgrade_hold'] is not True
                or hold['profile'] != PROFILE or hold['extension'] != EXTENSION
                or hold['error'] != reason
                or not _commit(hold['source_revision']) or hold['source_sha256'] != row['old_source_sha256']):
            raise ValueError('invalid exact profile-upgrade hold')
        repo, path = hold['repo'], hold['path']
        if (not _matches(repo, r'[A-Za-z0-9_.-]{1,128}/[A-Za-z0-9_.-]{1,128}')
                or not _matches(path, r'[^\x00-\x1f\\\ud800-\udfff]{1,2048}')
                or any(p in {'', '.', '..'} for p in (repo + '/' + path).split('/'))
                or not path.lower().endswith('.' + EXTENSION) or hold['key'] != f'{repo}\0{path}'):
            raise ValueError('invalid profile-upgrade hold key')
        if (not _fields(candidate, CANDIDATE_FIELDS) or not _commit(candidate['source_revision'])
                or candidate['source_sha256'] != row['old_source_sha256'] or not web._is_sha(candidate['sha256'])
                or type(candidate['bytes']) is not int or not 0 < candidate['bytes'] <= MAX_DOCUMENT):
            raise ValueError('invalid pinned hold candidate')
        if hold['key'] in keys or candidate['sha256'] in hashes:
            raise ValueError('duplicate hold key or candidate SHA-256')
        keys.add(hold['key'])
        hashes.add(candidate['sha256'])
        total += candidate['bytes']
    if total > MAX_CONTENT:
        raise ValueError('hold candidate content exceeds aggregate byte limit')
    return value


def parse(raw, expected_sha256):
    """Parse exact UTF-8 registry bytes against an independently supplied SHA-256."""
    return Registry(raw, expected_sha256)


def _registered_rows(registry):
    if not isinstance(registry, Registry):
        raise ValueError('authenticated hold registry required')
    return {row['failure']['key']: row for row in registry['rows']}


def _match_rows(registered, rows, fields):
    if not isinstance(rows, list) or len(rows) != len(registered):
        raise ValueError('managed hold row count differs from registered scope')
    seen, ids = set(), set()
    for row in rows:
        if not _fields(row, fields) or not _matches(row['id'], r'[0-9]{4}') or row['id'] in ids:
            raise ValueError('invalid or duplicate managed hold row ID or fields')
        new = row['new']
        web._web_row(new, old=False)
        key = new['key']
        if key not in registered or key in seen:
            raise ValueError('managed hold rows are not a one-to-one registry match')
        hold = registered[key]
        candidate = hold['candidate']
        # The producer's preparation revision is NOT the canonical watermark.
        if (new['extension'] != EXTENSION or new.get('profile') != PROFILE
                or new['source_sha256'] != hold['old_source_sha256']
                or (new['sha256'], new['bytes']) != (candidate['sha256'], candidate['bytes'])):
            raise ValueError('managed row differs from the pinned hold candidate')
        seen.add(key)
        ids.add(row['id'])
        yield key, row, hold


def validate_approval(registry, approval):
    """Check the managed approval.json; live old object and bytes are checked later."""
    registered = _registered_rows(registry)
    if (not _fields(approval, {'version', 'rows', 'expected_generation', 'timestamp'})
            or type(approval['version']) is not int or approval['version'] != 1
            or approval['expected_generation'] != registry['reader_parent']['generation']):
        raise ValueError('managed hold approval Reader parent or envelope differs')
    timestamp = approval['timestamp']
    try:
        if not isinstance(timestamp, str) or len(timestamp) > 40 or datetime.fromisoformat(timestamp).utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError('invalid managed hold approval timestamp') from None
    for _, row, _ in _match_rows(registered, approval['rows'], {'id', 'new', 'expected_old_object'}):
        if (not _matches(row['expected_old_object'], r'documents/web/html/[a-f0-9]{64}/document\.html')
                or row['expected_old_object'] == row['new']['object']):
            raise ValueError('invalid managed hold approval old object')


def _reader_parent(pointer):
    catalog = pointer.get('catalog') if isinstance(pointer, dict) else None
    if not isinstance(catalog, dict):
        raise ValueError('missing Reader parent catalog')
    return {'generation': pointer.get('generation'), 'catalog_sha256': catalog.get('sha256')}


def validate_review(registry, review, parents):
    """Match authenticated registry, managed review and exact canonical parent bytes.

    Works with the canonical pin in either its on-disk or the managed parser's
    normalized shape. The existing managed parser checks successor, ZIP member
    bytes and manifests; this adds exact hold/failure and candidate matching.
    """
    registered = _registered_rows(registry)
    if (not isinstance(review, dict) or review.get('kind') != 'reader-web-managed-v1'
            or review.get(DIGEST_FIELD) != registry.sha256 or type(review.get('selected')) is not int
            or review['selected'] != len(registered)
            or _reader_parent(review.get('parent_pointer')) != registry['reader_parent']):
        raise ValueError('managed hold review digest, scope or Reader parent differs')
    if not _fields(parents, {EXTENSION}):
        raise ValueError('managed hold canonical parent must be the HTML index only')
    raw = parents[EXTENSION]
    if not isinstance(raw, bytes) or not 0 < len(raw) <= web.MAX_CANONICAL:
        raise ValueError('invalid managed hold canonical parent size')
    pin = review.get('canonical')
    pin, sha_field = (pin[EXTENSION], 'parent_sha256') if _fields(pin, {EXTENSION}) else (pin, 'before_sha256')
    if (not isinstance(pin, dict) or pin.get('path') != f'documents/web/{EXTENSION}/index.json'
            or pin.get(sha_field) != _digest(raw)):
        raise ValueError('managed hold canonical parent pin differs')
    parent = web._json(raw, 'managed hold canonical parent')
    if (not isinstance(parent, dict) or type(parent.get('version')) is not int or parent['version'] != 1
            or parent.get('kind') != web.KIND
            or parent.keys() - {'kind', 'version', 'files', 'failures', 'checkpoint_operation'}):
        raise ValueError('invalid managed hold canonical parent schema')
    files, failures = web._rows(parent, 'files'), web._rows(parent, 'failures')
    fields = {'id', 'old', 'new', 'old_manifest_sha256', 'new_manifest_sha256'}
    for key, row, hold in _match_rows(registered, review.get('rows'), fields):
        old, new = row['old'], row['new']
        web._web_row(old, old=True)
        if (old['key'] != key or old['extension'] != EXTENSION or not web._json_equal(files.get(key), old)
                or old['source_sha256'] != hold['old_source_sha256'] or new['source_revision'] != old['source_revision']
                or old['object'] == new['object'] or old['sha256'] == new['sha256']
                or not all(web._is_sha(row[k]) for k in ('old_manifest_sha256', 'new_manifest_sha256'))):
            raise ValueError('managed hold old route or exact same-source successor differs')
        if not web._json_equal(failures.get(key), hold['failure']):
            raise ValueError('actual canonical profile-upgrade failure missing or changed')


def _count(n, minimum=0):
    return type(n) is int and n >= minimum


def _rendered(facts):
    return (type(facts['width']) is int and facts['width'] == WIDTH
            and type(facts['scroll']) is int and WIDTH <= facts['scroll'] <= MAX_SCROLL
            and _count(facts['images']) and _count(facts['decoded']) and facts['images'] == facts['decoded'])


def validate_replay(registry, report):
    """Require both pinned code SHAs and the exact candidate SHA set on each site."""
    expected = {r['candidate']['sha256']: r['require_clean'] for r in _registered_rows(registry).values()}
    if not _fields(report, {'search', 'hf-search'}):
        raise ValueError('hold admission replay requires the exact two sites')
    for name, commit_field in (('search', 'pages_commit'), ('hf-search', 'space_commit')):
        site = report[name]
        if not _fields(site, {'sha', 'documents'}) or site['sha'] != registry[commit_field]:
            raise ValueError(f'hold admission replay code SHA differs: {name}')
        if not _fields(site['documents'], expected.keys()):
            raise ValueError(f'hold admission replay candidate SHA set differs: {name}')
        for digest, facts in site['documents'].items():
            if (not isinstance(facts, dict) or not ARCHIVE_FACTS <= facts.keys() <= ARCHIVE_FACTS | {'clean'}
                    or facts['passed'] is not True or facts['mismatch'] != []
                    or not _count(facts['blocked_requests']) or facts['blocked_requests'] != 0 or not _rendered(facts)):
                raise ValueError(f'hold admission archive replay failed: {name}/{digest}')
            if expected[digest] or 'clean' in facts:
                clean = facts.get('clean')
                if (not _fields(clean, CLEAN_FACTS) or clean['exact'] is not True
                        or not _count(clean['posts'], 1) or not _count(clean['rendered'], 1)
                        or clean['posts'] != clean['rendered'] or not _rendered(clean)):
                    raise ValueError(f'hold admission clean replay failed: {name}/{digest}')


def validate_proof(registry, review, parents, *, search_generation, pages_commit, space_commit,
                   reader_pointer, routes, report, prior_proof=_NEW_PROOF):
    """Return a JSON proof from trusted observations, or recheck a prior proof.

    Supply fresh canonical bytes in ``parents``, the observed Reader pointer,
    selected old projected routes keyed by canonical key, independently proved
    Search/Pages/Space values and the trusted exact-candidate replay report.
    Apply must pass an independently sealed ``prior_proof`` and keep the normal
    managed gates. A matching proof does not itself establish freshness.
    """
    validate_review(registry, review, parents)
    if ((search_generation, pages_commit, space_commit) !=
            (registry['search_generation'], registry['pages_commit'], registry['space_commit'])
            or not web._json_equal(reader_pointer, review['parent_pointer'])):
        raise ValueError('observed Search, Pages, Space or Reader parent differs')
    expected_routes = {r['old']['key']: {'s': 2, 'm': 'h', 'p': r['old']['object'], 'b': r['old']['bucket']}
                       for r in review['rows']}
    if not web._json_equal(routes, expected_routes):
        raise ValueError('observed hold admission old route set differs')
    validate_replay(registry, report)
    proof = {'version': 1, 'scope': registry['scope'], 'selected': review['selected'],
             DIGEST_FIELD: registry.sha256, 'search_generation': search_generation,
             'pages_commit': pages_commit, 'space_commit': space_commit,
             'reader_pointer_sha256': _seal(reader_pointer), 'canonical_parent_sha256': _digest(parents[EXTENSION]),
             'review_rows_sha256': _seal(review['rows']), 'replay_sha256': _seal(report)}
    if prior_proof is not _NEW_PROOF and not web._json_equal(prior_proof, proof):
        raise ValueError('hold admission proof differs from prior proof')
    return proof
