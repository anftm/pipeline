"""A CCRD candidate plan cannot authorize four-source publication."""
import copy
import hashlib
import json
import unittest
from urllib.parse import quote

from scripts import ccrd_candidate_preflight as preflight
from scripts import ccrd_source_override as source
from tests.test_ccrd_lifecycle import CcrdLifecycleTests

OLD = '原文本'.encode(); NEW = '新文本'.encode(); REV = 'a' * 40
URL = f'{source.UPSTREAM_REPO}/{REV}/{quote(source.UPSTREAM_PATHS[0], safe="/")}'


def identity(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def source_receipt(raw=NEW):
    return {'url': URL, 'revision': REV, **identity(raw), 'source_stable': True}


class CcrdCandidatePreflightTests(unittest.TestCase):
    def setUp(self):
        fixture = CcrdLifecycleTests('test_four_source_pointer_retains_all_databases_metadata_and_txt_archives')
        fixture.setUp(); self.manifest = fixture.four_source_pointer()
        self.manifest['source_overrides'] = {str(source.CORPUS_PATH): identity(OLD)}
        self.pointer = json.dumps(self.manifest, ensure_ascii=False).encode()
        self.observed = {'generation': self.manifest['generation'], 'cw0': identity(OLD), 'cw0_bytes_verified': True,
                         'status_observed_twice': True, 'loaded_sources': ['CCRD', 'CW', 'RMRB', 'PLA']}

    def make(self):
        return preflight.plan(self.pointer, OLD, NEW, self.observed, source_url=URL, source_revision=REV,
                              source_observation=source_receipt())

    def test_plan_preserves_all_other_sources_and_never_authorizes_publish(self):
        result = self.make()
        self.assertEqual(set(result['preserved_database_identities']), {'CCRD', 'RMRB', 'PLA'})
        self.assertEqual(result['parent_metadata'], self.manifest['metadata'])
        self.assertEqual(result['parent_archives'], self.manifest['archives'])
        self.assertEqual(result['parent_pointer_sha256'], hashlib.sha256(self.pointer).hexdigest())
        self.assertEqual(result['new_text'], identity(NEW))
        self.assertEqual(result['new_source']['revision'], REV)
        self.assertFalse(result['publication_ready']); self.assertEqual(result['remote_writes'], 0)

    def test_stale_parent_or_nonready_consumer_stops(self):
        for field, value in (('generation', 'wrong'), ('cw0_bytes_verified', False),
                             ('cw0', identity(NEW)), ('loaded_sources', ['CCRD', 'CW'])):
            observed = {**self.observed, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                preflight.plan(self.pointer, OLD, NEW, observed, source_url=URL, source_revision=REV,
                               source_observation=source_receipt())
        with self.assertRaisesRegex(ValueError, 'old CW:0'):
            preflight.plan(self.pointer, b'wrong', NEW, self.observed, source_url=URL, source_revision=REV,
                           source_observation=source_receipt())
        with self.assertRaisesRegex(ValueError, 'has not changed'):
            preflight.plan(self.pointer, OLD, OLD, self.observed, source_url=URL, source_revision=REV,
                           source_observation=source_receipt(OLD))

    def test_claimed_source_must_match_the_observed_exact_bytes_and_revision(self):
        for mutation in ({'sha256': 'f' * 64}, {'bytes': len(NEW) + 1}, {'revision': 'b' * 40},
                         {'url': URL + '?changed'}, {'source_stable': False}):
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'source observation'):
                preflight.plan(self.pointer, OLD, NEW, self.observed, source_url=URL, source_revision=REV,
                               source_observation={**source_receipt(), **mutation})

    def test_unpinned_or_foreign_source_and_malformed_manifest_stop(self):
        for url, revision in ((URL, 'main'), (URL.replace(REV, 'b' * 40), REV),
                              ('https://example.invalid/' + REV + '/text', REV),
                              (f'{source.UPSTREAM_REPO}/{REV}/unapproved.txt', REV)):
            with self.subTest(url=url), self.assertRaises(ValueError):
                preflight.plan(self.pointer, OLD, NEW, self.observed, source_url=url, source_revision=revision,
                               source_observation=source_receipt())
        broken = copy.deepcopy(self.manifest); broken['databases'].pop('PLA')
        with self.assertRaises(ValueError):
            preflight.plan(json.dumps(broken).encode(), OLD, NEW, self.observed, source_url=URL, source_revision=REV,
                           source_observation=source_receipt())
        with self.assertRaises(UnicodeDecodeError):
            preflight.plan(self.pointer, OLD, b'\xff', self.observed, source_url=URL, source_revision=REV,
                           source_observation=source_receipt())


if __name__ == '__main__':
    unittest.main()
