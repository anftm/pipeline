"""Offline contract tests; no credentials, network, or shared fixtures."""
from copy import deepcopy
from hashlib import sha256
import json
import unittest

from scripts import reader_web_hold_admission as admission, reader_web_hold_cleanup as cleanup
from scripts import reader_web_batch_plan as plan, reader_web_index_guard as index
from types import SimpleNamespace
from unittest.mock import Mock, patch


def digest(raw):
    return sha256(raw).hexdigest()


def encode(obj):
    return json.dumps(obj, ensure_ascii=False).encode('utf-8')


def registry(obj):
    raw = encode(obj)
    return admission.parse(raw, digest(raw))


class HoldAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.source = {'version': 1, 'scope': 'replay_holds', 'search_generation': '9'*64,
                       'pages_commit': '8'*40, 'space_commit': '7'*40,
                       'reader_parent': {'generation': '6'*64, 'catalog_sha256': '5'*64}, 'rows': []}
        self.pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': '6'*64,
                        'catalog': {'sha256': '5'*64, 'path': 'catalog.json'}}
        self.before = {'version': 1, 'kind': 'web-document-stream-index', 'files': [{'key': 'unrelated'}],
                       'failures': [{'key': 'not-in-cohort', 'error': 'unrelated'}]}
        self.report = {site: {'sha': self.source[field], 'documents': {}} for site, field in
                       [('search', 'pages_commit'), ('hf-search', 'space_commit')]}
        self.review = {'kind': 'reader-web-managed-v1', 'selected': 21, 'rows': [], 'parent_pointer': self.pointer}
        self.approval = {'version': 1, 'expected_generation': '6'*64, 'rows': [], 'timestamp': '2026-11-23T00:00:00+00:00'}
        self.routes = {}
        for i in range(21):
            path = f'目录/{i}.html'
            source, target = digest(path.encode()), digest(f'new:{i}'.encode())
            identity = {'key': 'example/books\0'+path, 'repo': 'example/books', 'path': path, 'extension': 'html',
                        'source_sha256': source}
            failure = {**identity, 'source_revision': '1'*40, 'profile': admission.PROFILE,
                       'error': 'reader_replay_mismatch', 'profile_upgrade_hold': True}
            # The preparation, canonical, and historical failure revisions are independent.
            size = 6 * 1024**2 if i == 0 else i + 10
            candidate = {'source_revision': '2'*40, 'source_sha256': source, 'sha256': target, 'bytes': size}
            require_clean = i < 19
            self.source['rows'].append({'failure': failure, 'candidate': candidate,
                                        'old_source_sha256': source, 'require_clean': require_clean})
            old_root = 'documents/web/html/'+digest(f'old-root:{i}'.encode())
            object_base = {**identity, 'source_revision': '3'*40, 'mode': 'document-stream',
                           'bucket': 'vomebook/reader-assets-v2'}
            old = {**object_base, 'object': old_root+'/document.html', 'manifest': old_root+'/document-manifest.json',
                   'sha256': digest(f'old:{i}'.encode()), 'bytes': 100, 'checkpointed_at': 'old'}
            new_root = 'documents/web/html/'+digest(f'{source}\0{target}'.encode())
            new = {**object_base, 'object': new_root+'/document.html', 'manifest': new_root+'/document-manifest.json',
                   'sha256': target, 'bytes': size, 'profile': admission.PROFILE}
            r = {'id': f'{i:04}', 'new': new}
            self.approval['rows'].append({**r, 'expected_old_object': old['object']})
            self.review['rows'].append({**r, 'old': old, 'old_manifest_sha256': '4'*64, 'new_manifest_sha256': '3'*64})
            self.before['files'].append(deepcopy(old))
            self.before['failures'].append(deepcopy(failure))
            self.routes[old['key']] = {'s': 2, 'm': 'h', 'p': old['object'], 'b': old['bucket']}
            facts = {'mismatch': [], 'images': 2, 'decoded': 2, 'width': 390, 'scroll': 391,
                     'blocked_requests': 0, 'passed': True}
            if require_clean:
                facts['clean'] = {'posts': 3, 'rendered': 3, 'exact': True, 'images': 1, 'decoded': 1,
                                  'width': 390, 'scroll': 390}
            for site in self.report.values():
                site['documents'][target] = deepcopy(facts)
        self.registry = registry(self.source)
        self.review[admission.DIGEST_FIELD] = self.registry.sha256
        self.parents = self.seal(self.before, self.review)

    @staticmethod
    def seal(before, review):
        raw = encode(before)
        review['canonical'] = {'path': 'documents/web/html/index.json', 'before_sha256': digest(raw)}
        return {'html': raw}

    def proof(self, **overrides):
        observations = {k: self.source[k] for k in ('search_generation', 'pages_commit', 'space_commit')}
        observations.update(reader_pointer=self.pointer, routes=self.routes, report=self.report)
        return admission.validate_proof(self.registry, self.review, self.parents, **(observations | overrides))

    def test_exact_rows_approval_review_replay_and_repeated_proof(self):
        self.approval['rows'].reverse()
        admission.validate_approval(self.registry, self.approval)
        admission.validate_review(self.registry, self.review, self.parents)
        admission.validate_replay(self.registry, self.report)
        before = deepcopy((self.source, self.approval, self.review, self.parents, self.report))
        proof = self.proof()
        self.assertEqual(self.proof(prior_proof=json.loads(encode(proof))), proof)
        for site in self.report.values():
            site['documents'] = dict(reversed(list(site['documents'].items())))
        self.assertEqual(self.proof(prior_proof=proof), proof)
        self.assertEqual(before, (self.source, self.approval, self.review, self.parents, self.report))
        self.review['canonical'] = {'html': {'path': 'documents/web/html/index.json',
                                             'parent_sha256': digest(self.parents['html'])}}
        admission.validate_review(self.registry, self.review, self.parents)

    def test_cleanup_preserves_routed_rows_and_unrelated_notices_after_readback(self):
        self.review['target_pointer']={'generation':'e'*64}
        pairs=[(r['old'],r['new']) for r in self.review['rows']]
        raw=plan.canonical(self.parents,pairs,'a'*32,managed=True)['html']
        data={admission.MEMBER:encode(self.source),'target-html.json':raw}
        publication={'generation':'e'*64,'failure_notices_retained':True,'hold_admission':self.proof()}
        rb={'generation':'e'*64,'both_projections_verified':True,'old_and_new_verified':21,'deleted_objects':0}
        state=[raw]; cps=Mock(); hub=Mock(); journal=Mock(); journal.read.return_value=None; journal.claim.return_value=True
        cps.read.side_effect=lambda *_:SimpleNamespace(raw=state[0],etag='"etag"')
        hub.read_bytes.side_effect=lambda *_:state[0]
        def write(_b,_p,val,_e,**_kw):state[0]=index._encode(val); return 'written'
        cps.write_once.side_effect=write
        with patch.object(cleanup.v3,'current',return_value=(self.review['target_pointer'],)):
            with self.assertRaises(ValueError):
                cleanup.cleanup(self.review,data,publication,rb|{'both_projections_verified':False},cps,hub,journal,guard=lambda:True)
            journal.claim.assert_not_called()
            result=cleanup.cleanup(self.review,data,publication,rb,cps,hub,journal,guard=lambda:True)
        self.assertEqual(result['removed_count'],21)
        self.assertEqual(json.loads(state[0])['files'],json.loads(raw)['files'])
        self.assertEqual(json.loads(state[0])['failures'],[self.before['failures'][0]])
        self.assertEqual(journal.claim.call_count,1)
        cps.write_once.assert_called_once()

    def test_budget_scope_is_separate_and_cannot_admit_combined_batch(self):
        budget = deepcopy(self.source['rows'][0])
        budget['failure']['error'] = 'output_budget'
        budget_scope = self.source | {'scope': 'output_budget', 'rows': [budget]}
        approved = registry(budget_scope)
        for mixed in (self.source | {'rows': [budget]+self.source['rows'][1:]},
                      self.source | {'rows': [budget]+self.source['rows']},
                      budget_scope | {'rows': [budget, self.source['rows'][1]]},
                      self.source | {'rows': self.source['rows'][:1]}):
            with self.assertRaises(ValueError):
                registry(mixed)
        key, sha = budget['failure']['key'], budget['candidate']['sha256']
        approval = self.approval | {'rows': self.approval['rows'][:1]}
        review = self.review | {'rows': self.review['rows'][:1], 'selected': 1, admission.DIGEST_FIELD: approved.sha256}
        parents = self.seal(self.before | {'failures': [budget['failure'], *self.before['failures'][2:]]}, review)
        report = {name: site | {'documents': {sha: site['documents'][sha]}} for name, site in self.report.items()}
        admission.validate_approval(approved, approval)
        admission.validate_review(approved, review, parents)
        admission.validate_replay(approved, report)
        with self.assertRaises(ValueError):
            admission.validate_replay(approved, self.report)
        with self.assertRaises(ValueError):
            admission.validate_replay(self.registry, report)
        proof = admission.validate_proof(approved, review, parents, search_generation=self.source['search_generation'],
                                         pages_commit=self.source['pages_commit'], space_commit=self.source['space_commit'],
                                         reader_pointer=self.pointer, routes={key: self.routes[key]}, report=report)
        self.assertEqual((proof['scope'], proof['selected']), ('output_budget', 1))

    def test_registry_is_addressed_by_raw_digest_and_rows_are_copies(self):
        raw = encode(self.source)
        for tampered, pin in [(raw+b'\n', digest(raw)), (raw, '0'*64), (raw, digest(raw).upper()),
                              (bytearray(raw), digest(raw)), (raw, None)]:
            with self.subTest(pin=pin), self.assertRaisesRegex(ValueError, 'digest or size'):
                admission.parse(tampered, pin)
        self.registry['rows'][0]['failure']['error'] = 'changed'
        self.assertEqual(self.registry['rows'][0]['failure']['error'], 'reader_replay_mismatch')
        self.assertEqual(self.registry.sha256, digest(raw))
        with self.assertRaises(ValueError):
            admission.validate_review(self.source, self.review, self.parents)

    def test_strict_json_and_exact_size_bound(self):
        raw = encode(self.source)
        padded = raw.ljust(admission.MAX_REGISTRY, b' ')
        self.assertEqual(admission.parse(padded, digest(padded)).sha256, digest(padded))
        for bad in (b'', b'null', b'\xff', b'\xef\xbb\xbf'+raw, raw+b' true', padded+b' ',
                    b'['*10000+b']'*10000, raw.replace('目录/0.html'.encode(), b'\\ud800.html'),
                    raw.replace(b'"version": 1', b'"version": 1, "version": 1'),
                    raw.replace(b'"bytes":', b'"bytes": 0, "bytes":', 1),
                    raw.replace(b'"version": 1', b'"version": true'), raw.replace(b'"version": 1', b'"version": 1.0'),
                    raw.replace(b'"version": 1', b'"version": 1e99'), raw.replace(b'"version": 1', b'"version": NaN'),
                    raw.replace(b'"version": 1', b'"version": Infinity'),
                    raw.replace(b'"version": 1', b'"version": '+b'9'*30)):
            with self.subTest(raw_prefix=bad[:60], size=len(bad)), self.assertRaises(ValueError):
                admission.parse(bad, digest(bad))

    def test_registry_rejects_tampered_shape(self):
        edits = [
            lambda d: d.update(extra=True), lambda d: d.update(search_generation='missing'),
            lambda d: d.update(scope='all'), lambda d: d.update(scope=['replay_holds']),
            lambda d: d.update(pages_commit='8'*64), lambda d: d.update(space_commit='7'*39),
            lambda d: d['reader_parent'].pop('catalog_sha256'),
            lambda d: d['rows'].pop(), lambda d: d['rows'].append(deepcopy(d['rows'][0])),
            lambda d: d['rows'][1].update(failure=d['rows'][0]['failure'], old_source_sha256=d['rows'][0]['old_source_sha256'],
                                         candidate=d['rows'][1]['candidate'] | {'source_sha256': d['rows'][0]['old_source_sha256']}),
            lambda d: d['rows'][1]['candidate'].update(sha256=d['rows'][0]['candidate']['sha256']),
            lambda d: d['rows'][0].update(old_source_sha256='2'*64),
            lambda d: d['rows'][0].update(require_clean=1),
            lambda d: d['rows'][0]['candidate'].update(bytes=False),
            lambda d: d['rows'][0]['candidate'].update(bytes=admission.MAX_DOCUMENT+1),
            lambda d: d['rows'][1]['candidate'].update(bytes=admission.MAX_DOCUMENT),
            lambda d: d['rows'][0]['candidate'].update(source_sha256='2'*64),
            lambda d: d['rows'][0]['candidate'].update(source_revision='main'),
            lambda d: d['rows'][0]['failure'].update(source_revision='main'),
            lambda d: d['rows'][0]['failure'].update(extension='mht'),
            lambda d: d['rows'][0]['failure'].update(profile_upgrade_hold=1),
            lambda d: d['rows'][0]['failure'].pop('error'),
            lambda d: d['rows'][0]['failure'].update(error=''),
            lambda d: d['rows'][0]['failure'].update(path='../../invalid.html'),
            lambda d: d['rows'][0]['failure'].update(error='output_budget'),
            lambda d: d['rows'][0]['failure'].update(profile='web-utf8-typography-v3'),
        ]
        for i, edit in enumerate(edits):
            altered = deepcopy(self.source)
            edit(altered)
            with self.subTest(edit=i), self.assertRaises(ValueError):
                registry(altered)

    def test_content_budget_is_twelve_mib_even_with_valid_individual_sizes(self):
        exact = deepcopy(self.source)
        first, second, *rest = [r['candidate'] for r in exact['rows']]
        second['bytes'] = 12 * 1024**2 - first['bytes'] - sum(c['bytes'] for c in rest)
        registry(exact)
        second['bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'aggregate byte limit'):
            registry(exact)

    def test_approval_rejects_tampered_candidates_or_cohort(self):
        edits = [
            lambda d: d.update(expected_generation='0'*64), lambda d: d.update(timestamp=None),
            lambda d: d['rows'].pop(), lambda d: d['rows'].append(deepcopy(d['rows'][0])),
            lambda d: d['rows'][0].update(id=d['rows'][1]['id']),
            lambda d: d['rows'][0].update(new=deepcopy(d['rows'][1]['new'])),
            lambda d: d['rows'][0].update(expected_old_object=d['rows'][0]['new']['object']),
            lambda d: d['rows'][0].update(expected_old_object='another/index.json'),
            lambda d: d['rows'][0]['new'].pop('profile'),
            lambda d: d['rows'][0]['new'].update(bytes=5),
        ]
        for i, edit in enumerate(edits):
            approval = deepcopy(self.approval)
            edit(approval)
            with self.subTest(edit=i), self.assertRaises(ValueError):
                admission.validate_approval(self.registry, approval)
        mixed = deepcopy(self.approval)
        new = mixed['rows'][0]['new']
        new.update(path='unregistered.html', key=new['repo']+'\0unregistered.html')
        with self.assertRaisesRegex(ValueError, 'one-to-one'):
            admission.validate_approval(self.registry, mixed)
        new.update(path=self.approval['rows'][0]['new']['path'], key=self.approval['rows'][0]['new']['key'])
        new['sha256'] = 'a'*64
        root = 'documents/web/html/'+digest(f"{new['source_sha256']}\0{new['sha256']}".encode())
        new.update(object=root+'/document.html', manifest=root+'/document-manifest.json')
        with self.assertRaisesRegex(ValueError, 'pinned hold candidate'):
            admission.validate_approval(self.registry, mixed)

    def test_review_checks_actual_full_failure_and_exact_old_route(self):
        for field, changed in [('error', 'output_budget'), ('profile_upgrade_hold', 1),
                               ('source_revision', 'c'*40), ('source_sha256', 'c'*64), ('extra', 'new')]:
            parent, review = deepcopy((self.before, self.review))
            parent['failures'][1][field] = changed
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'failure missing or changed'):
                admission.validate_review(self.registry, review, self.seal(parent, review))
        parent, review = deepcopy((self.before, self.review))
        parent['failures'].pop(1)
        with self.assertRaisesRegex(ValueError, 'failure missing or changed'):
            admission.validate_review(self.registry, review, self.seal(parent, review))
        for field, changed in [('source_revision', '2'*40), ('sha256', 'c'*64), ('bytes', 1)]:
            parent, review = deepcopy((self.before, self.review))
            parent['files'][1][field] = changed
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'old route'):
                admission.validate_review(self.registry, review, self.seal(parent, review))
        for review in (self.review | {admission.DIGEST_FIELD: '2'*64}, self.review | {'selected': 21.0},
                       self.review | {'parent_pointer': self.pointer | {'catalog': {'sha256': '2'*64}}}):
            with self.assertRaises(ValueError):
                admission.validate_review(self.registry, review, self.parents)
        with self.assertRaises(ValueError):
            admission.validate_review(self.registry, self.review, self.parents | {'mht': b'{}'})
        with self.assertRaisesRegex(ValueError, 'canonical parent pin'):
            admission.validate_review(self.registry, self.review, {'html': self.parents['html']+b' '})

    def test_replay_requires_both_exact_sites_and_candidate_hashes(self):
        for edit in [lambda d: d.pop('search'), lambda d: d.update(space=d['hf-search']),
                     lambda d: d['search'].update(sha='0'*40),
                     lambda d: d['hf-search']['documents'].popitem(),
                     lambda d: d['hf-search']['documents'].update(extra={}),
                     lambda d: d['search']['documents'].update({'0'*64: d['search']['documents'].popitem()[1]})]:
            report = deepcopy(self.report)
            edit(report)
            with self.assertRaises(ValueError):
                admission.validate_replay(self.registry, report)

    def test_replay_checks_archive_and_clean_facts_not_just_passed(self):
        digest = self.source['rows'][0]['candidate']['sha256']
        for mode, field, value in [
            ('archive', 'passed', 1), ('archive', 'mismatch', ['body']), ('archive', 'mismatch', None),
            ('archive', 'blocked_requests', 1), ('archive', 'blocked_requests', False),
            ('archive', 'decoded', 1), ('archive', 'images', True), ('archive', 'scroll', 392),
            ('archive', 'scroll', 389), ('archive', 'width', 390.0), ('archive', 'clean', None),
            ('clean', 'exact', False), ('clean', 'posts', 0), ('clean', 'rendered', 2),
            ('clean', 'decoded', 0), ('clean', 'width', 389), ('clean', 'scroll', 400),
        ]:
            report = deepcopy(self.report)
            facts = report['hf-search']['documents'][digest]
            (facts if mode == 'archive' else facts['clean'])[field] = value
            with self.subTest(mode=mode, field=field, value=value), self.assertRaises(ValueError):
                admission.validate_replay(self.registry, report)
        report = deepcopy(self.report)
        report['search']['documents'][digest].pop('clean')
        with self.assertRaisesRegex(ValueError, 'clean replay failed'):
            admission.validate_replay(self.registry, report)

    def test_positive_proof_rejects_drift_and_cannot_be_reused_for_new_parents(self):
        proof = self.proof()
        changes = [{'search_generation': 'a'*64}, {'pages_commit': 'a'*40}, {'space_commit': 'a'*40},
                   {'reader_pointer': self.pointer | {'other': True}}, {'routes': {}}]
        for delta in changes:
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self.proof(prior_proof=proof, **delta)
        for bad in (None, proof | {'replay_sha256': '0'*64}, proof | {'scope': 'output_budget'}):
            with self.assertRaisesRegex(ValueError, 'prior proof'):
                self.proof(prior_proof=bad)
        report = deepcopy(self.report)
        next(iter(report['search']['documents'].values()))['scroll'] = 390
        with self.assertRaisesRegex(ValueError, 'prior proof'):
            self.proof(prior_proof=proof, report=report)
        self.before['failures'][0]['error'] = 'changed outside cohort'
        self.parents = self.seal(self.before, self.review)
        with self.assertRaisesRegex(ValueError, 'prior proof'):
            self.proof(prior_proof=proof)
        self.before['failures'][1]['error'] = 'changed inside cohort'
        self.parents = self.seal(self.before, self.review)
        with self.assertRaisesRegex(ValueError, 'failure missing or changed'):
            self.proof(prior_proof=proof)


if __name__ == '__main__':
    unittest.main()
