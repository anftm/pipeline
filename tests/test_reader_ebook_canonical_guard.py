"""Pure exact EPUB replacement and one-shot publication with memory-only adapters."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import unittest

from scripts import reader_ebook_canonical_guard as subject

OPERATION = '1' * 32
ETAG = '"parent"'
PINS = {'review': 'a' * 64, 'stage': 'b' * 64, 'pages': 'c' * 64}


def row_and_spec(repo, path, source_sha, profile):
    old_root = f'{subject.ROOT}/{source_sha}/' + 'd' * 16
    new_root = f'{subject.ROOT}/{source_sha}/' + 'e' * 16
    old_search = f'{old_root}/{subject.SEARCH}' if profile else None
    key = f'{repo}\0{path}'
    spec = subject.Replacement(key, source_sha, old_root, f'{old_root}/{subject.MANIFEST}', old_search, '1' * 64,
                               new_root, f'{new_root}/{subject.MANIFEST}', f'{new_root}/{subject.SEARCH}', '2' * 64, 9)
    row = {'key': key, 'repo': repo, 'path': path, 'source_sha256': source_sha, 'root': old_root,
           'manifest': spec.old_manifest, 'bucket': subject.BUCKET, 'source_extension': 'epub', 'extension': 'epub',
           'source_revision': '4' * 40, 'source_bytes': 100, 'mode': 'chapter-stream', 'chapter_count': 8,
           'checkpointed_at': '2026-01-01T00:00:00Z', 'processing_roots': ['unchanged']}
    if profile:
        row.update(profile=profile, search_index=old_search)
    return row, spec


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


class MemoryStores:
    def __init__(self, raw, etag):
        self.live = raw, etag
        self.claims = {}
        self.mutations = []
        self.after_claim = None
        self.claim_result = True
        self.cas_fault = None

    def read(self, bucket, path):
        assert bucket == subject.BUCKET
        if path == subject.INDEX:
            return self.live
        assert path.startswith(subject.PERMIT_PREFIX)
        return self.claims.get(path)

    def claim(self, bucket, path, seal):
        assert bucket == subject.BUCKET and path.startswith(subject.PERMIT_PREFIX)
        self.mutations.append(('claim', path))
        if path in self.claims:
            return False
        self.claims[path] = seal
        if self.after_claim:
            self.after_claim()
        return self.claim_result

    def compare_and_swap(self, bucket, path, raw, *, if_match):
        assert (bucket, path) == (subject.BUCKET, subject.INDEX)
        self.mutations.append(('cas', path, if_match))
        if if_match != self.live[1] or self.cas_fault == 'before':
            return False
        if self.cas_fault != 'bad-readback':
            self.live = raw, '"target"'
        if self.cas_fault == 'after':
            raise TimeoutError('lost CAS response')
        return True


class EbookCanonicalGuardTests(unittest.TestCase):
    def setUp(self):
        first, one = row_and_spec('org/books', '书/one.epub', 'a' * 64, 'epub-chapters-v9-cover-toc')
        second, two = row_and_spec('org/other', 'two.EPUB', 'b' * 64, None)
        self.reviewed = (one, two)
        self.parent = {'version': 1, 'kind': subject.KIND, 'files': [second, {'key': 'other', 'hint': 1}, first],
                       'failures': [{'key': 'failed', 'error': 'keep'}], 'extra': {'enabled': True},
                       'checkpoint_operation': '0' * 32}
        self.parent_raw = json.dumps(self.parent).encode()  # Bind raw bytes, not re-serialized JSON.
        self.target_raw = self.build()
        self.claim_path = subject.PERMIT_PREFIX + sha256(self.parent_raw).hexdigest() + '.json'
        self.io = MemoryStores(self.parent_raw, ETAG)
        self.denied = None

    def build(self, **overrides):
        return subject.build_successor(**({'parent_raw': self.parent_raw, 'reviewed': self.reviewed,
                                          'operation_id': OPERATION} | overrides))

    def validate(self, target_raw):
        return subject.validate_successor(self.parent_raw, target_raw, reviewed=self.reviewed, operation_id=OPERATION)

    def apply(self, **overrides):
        return subject.apply(self.io, self.io, **({'parent_raw': self.parent_raw, 'parent_etag': ETAG,
             'target_raw': self.target_raw, 'reviewed': self.reviewed, 'operation_id': OPERATION,
             'prerequisites': PINS, 'guard': lambda phase: phase != self.denied} | overrides))

    def test_pure_exact_successor_for_two_keys_and_legacy_missing_search(self):
        self.validate(self.target_raw)
        expected = deepcopy(self.parent)
        for row, spec in zip((expected['files'][2], expected['files'][0]), self.reviewed, strict=True):
            row.update(profile=subject.EPUB_PROFILE, root=spec.new_root, manifest=spec.new_manifest,
                       search_index=spec.new_search_index, chapter_count=spec.chapter_count)
        expected['checkpoint_operation'] = OPERATION
        self.assertEqual(self.target_raw, encode(expected))
        self.assertEqual(self.io.mutations, [])
        no_failures = {k: v for k, v in self.parent.items() if k != 'failures'}
        self.assertNotIn('failures', json.loads(self.build(parent_raw=encode(no_failures))))

    def test_larger_reviewed_cohort_is_explicit_not_format_wide(self):
        pairs = [row_and_spec('org/books', f'{i}.epub', f'{i:064x}', 'epub-chapters-v8-bucket') for i in range(20)]
        rows, specs = zip(*pairs, strict=True)
        parent = {**self.parent, 'files': [*rows, *self.parent['files']]}
        target = json.loads(self.build(parent_raw=encode(parent), reviewed=specs))
        self.assertTrue(all(r['profile'] == subject.EPUB_PROFILE for r in target['files'][:20]))
        self.assertEqual(target['files'][20:], self.parent['files'])

    def test_exact_validation_rejects_any_out_of_scope_mutation_before_claim(self):
        for mutate in (lambda v: v['files'][0].update(source_revision='drift'),
                       lambda v: v['files'][0].update(checkpointed_at='drift'),
                       lambda v: v['files'][0].update(processing_roots=[]),
                       lambda v: v['files'][0].update(bucket='wrong'),
                       lambda v: v['files'][0].update(profile='kindle-chapters-v10-native-navigation'),
                       lambda v: v['files'][0].update(chapter_count=10),
                       lambda v: v['files'][1].update(hint=True),
                       lambda v: v['files'].reverse(), lambda v: v['files'].pop(),
                       lambda v: v['files'].append({'key': 'foreign'}),
                       lambda v: v['failures'].clear(), lambda v: v['extra'].update(enabled=1),
                       lambda v: v.update(checkpoint_operation='f' * 32)):
            target = json.loads(self.target_raw)
            mutate(target)
            with self.assertRaisesRegex(ValueError, 'exact reviewed EPUB successor'):
                self.apply(target_raw=encode(target))
        with self.assertRaises(ValueError):
            self.validate(json.dumps(json.loads(self.target_raw)).encode())
        self.assertEqual(self.io.mutations, [])

    def test_invalid_reviewed_keys_roots_sha_or_formats(self):
        one, two = self.reviewed
        foreign_old = one.old_root.replace(one.source_sha256, 'f' * 64)
        for overrides in ({'key': 'org/books\0unreviewed.epub'}, {'source_sha256': 'f' * 64},
                          {'old_root': foreign_old}, {'old_search_index': None}, {'old_manifest': two.old_manifest},
                          {'new_root': two.new_root, 'new_manifest': two.new_manifest}, {'chapter_count': True},
                          {'new_search_index': one.new_root + '/../epub-search-index.json.gz'},
                          {'new_search_index': None}, {'old_manifest_sha256': 'not sha'},
                          {'new_manifest_sha256': one.old_manifest_sha256}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.build(reviewed=[replace(one, **overrides)])
        for specs in ([], [one, one], [asdict(one)]):
            with self.assertRaises(ValueError):
                self.build(reviewed=specs)
        for changes in ({'source_extension': 'mobi'}, {'root': foreign_old}, {'profile': subject.EPUB_PROFILE},
                        {'repo': 'foreign/books'}, {'profile': 'unreviewed-version'}, {'bucket': 'foreign'}):
            parent = deepcopy(self.parent)
            parent['files'][2].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.build(parent_raw=encode(parent))
        parent = deepcopy(self.parent)
        parent['failures'].append({'key': one.key})
        with self.assertRaisesRegex(ValueError, 'failed'):
            self.build(parent_raw=encode(parent))

    def test_invalid_parent_json_duplicate_keys_and_operation(self):
        for raw in (self.parent_raw.replace(b'"version": 1', b'"version": true'),
                    self.parent_raw.replace(b'"version": 1', b'"version": 2, "version": 1'),
                    self.parent_raw.replace(b'"hint": 1', b'"hint": 1e1000'),
                    self.parent_raw.replace(b'"hint": 1', b'"hint": 1.00000000000000001')):
            with self.assertRaises(ValueError):
                self.build(parent_raw=raw)
        for field in ('files', 'failures'):
            parent = deepcopy(self.parent)
            parent[field].append(parent[field][0])
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                self.build(parent_raw=encode(parent))
        with self.assertRaisesRegex(ValueError, 'differ from the parent'):
            self.build(operation_id=self.parent['checkpoint_operation'])

    def test_success_requires_exact_readback_then_explicit_observation(self):
        self.assertEqual(self.apply(), 'written')
        self.assertEqual(self.io.mutations, [('claim', self.claim_path), ('cas', subject.INDEX, ETAG)])
        seal = json.loads(self.io.claims[self.claim_path])
        self.assertEqual(seal['prerequisites'], PINS)
        self.assertEqual(seal['target_sha256'], sha256(self.target_raw).hexdigest())
        self.assertEqual(self.apply(resume=True, reviewed=reversed(self.reviewed)), 'observed')
        with self.assertRaisesRegex(RuntimeError, 'already spent'):
            self.apply()
        for changes in ({'parent_etag': '"different"'}, {'prerequisites': PINS | {'pages': 'f' * 64}},
                        {'reviewed': [replace(self.reviewed[0], new_manifest_sha256='a' * 64), self.reviewed[1]]}):
            with self.assertRaisesRegex(RuntimeError, 'no matching spent'):
                self.apply(resume=True, **changes)
        self.io.live = json.dumps(json.loads(self.target_raw)).encode(), '"drift"'
        with self.assertRaisesRegex(RuntimeError, 'exact target not observed'):
            self.apply(resume=True)
        self.assertEqual(len(self.io.mutations), 2)

    def test_wrong_parent_or_no_permit_fails_without_mutations(self):
        self.io.live = self.target_raw, ETAG
        with self.assertRaisesRegex(RuntimeError, 'no matching spent'):
            self.apply(resume=True)
        for live in (self.io.live, (encode(self.parent), ETAG), (self.parent_raw, '"foreign"')):
            self.io.live = live
            with self.assertRaisesRegex(RuntimeError, 'drifted; no claim'):
                self.apply()
        with self.assertRaisesRegex(ValueError, 'strong S3 ETag'):
            self.apply(parent_etag=None)
        with self.assertRaisesRegex(ValueError, 'prerequisite SHA-256'):
            self.apply(prerequisites={'review': 'a' * 64})
        self.assertEqual(self.io.mutations, [])

    def test_drift_or_unconfirmed_claim_never_authorizes_cas(self):
        for scenario in ('drift', 'claim-unknown', 'claim-readback', 'guard'):
            with self.subTest(scenario=scenario):
                self.io = MemoryStores(self.parent_raw, ETAG)
                if scenario == 'drift':
                    self.io.after_claim = lambda: setattr(self.io, 'live', (self.parent_raw, '"rewritten"'))
                elif scenario == 'claim-unknown':
                    self.io.claim_result = False
                elif scenario == 'claim-readback':
                    self.io.after_claim = lambda: self.io.claims.update({self.claim_path: b'foreign'})
                else:
                    self.denied = 'put'
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.io.live = self.parent_raw, ETAG
                with self.assertRaisesRegex(RuntimeError, 'already spent'):
                    self.apply()
                self.assertEqual(self.io.mutations, [('claim', self.claim_path)])

    def test_claim_race_loser_cannot_write(self):
        def guard(phase):
            if phase == 'claim':
                self.assertEqual(self.apply(), 'written')
            return True

        with self.assertRaisesRegex(RuntimeError, 'permit not acquired'):
            self.apply(guard=guard)
        self.assertEqual([event[0] for event in self.io.mutations], ['claim', 'cas', 'claim'])
        self.assertEqual(self.apply(resume=True), 'observed')

    def test_uncertain_cas_or_guard_denial_can_only_observe(self):
        for scenario in ('before', 'after', 'bad-readback', 'denied-readback'):
            with self.subTest(scenario=scenario):
                self.io = MemoryStores(self.parent_raw, ETAG)
                self.io.cas_fault = scenario
                self.denied = 'readback' if scenario == 'denied-readback' else None
                with self.assertRaises((RuntimeError, TimeoutError)):
                    self.apply()
                self.denied = 'resume-readback'
                with self.assertRaisesRegex(RuntimeError, 'window not verified'):
                    self.apply(resume=True)
                self.denied = None
                if scenario in ('after', 'denied-readback'):
                    self.assertEqual(self.apply(resume=True), 'observed')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'exact target not observed'):
                        self.apply(resume=True)
                with self.assertRaisesRegex(RuntimeError, 'already spent'):
                    self.apply()
                self.assertEqual(self.io.mutations, [('claim', self.claim_path), ('cas', subject.INDEX, ETAG)])
