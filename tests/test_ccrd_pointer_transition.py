"""CCRD pointer CAS is not an activation, recovery, or writer fence."""
import unittest
from io import BytesIO
from unittest.mock import Mock

from scripts import ccrd_lifecycle
from scripts import ccrd_pointer_transition as pointer
from tests.test_ccrd_stage_manifest import CcrdStageManifestTests


class PointerMemory:
    def __init__(self, raw):
        self.raw = raw; self.etag = '"v1"'; self.writes = 0; self.lost = False

    def read_for_update(self):
        return self.raw, self.etag

    def compare_and_swap(self, *, raw, expected_etag):
        self.writes += 1
        if self.etag != expected_etag: raise ValueError('conflict')
        self.raw = raw; self.etag = '"v2"'
        if self.lost: raise TimeoutError('response lost')


class CcrdPointerTests(CcrdStageManifestTests):
    def args(self):
        plan, _store, _capture, _observe = self.prepared()
        raw = ccrd_lifecycle.canonical(plan['manifest'])
        evidence = {'manifest_sha256': plan['manifest_sha256'], 'generation': plan['generation'],
                    'activation': {'kind': 'ccrd-space-activation-observation', 'revision': 'a' * 40,
                                   'generation': plan['generation'], 'manifest_sha256': plan['manifest_sha256'],
                                   'runtime_observed_twice': True,
                                   'activation': {'kind': 'ccrd-index-activation', 'bucket': 'vomebook/ccrd-index',
                                                  'path': f"generations/{plan['generation']}/manifest.json",
                                                  'sha256': plan['manifest_sha256']}},
                    'consumer': {'generation': plan['generation'], 'cw0': plan['identity']['new_text'],
                                 'cw0_bytes_verified': True, 'status_observed_twice': True,
                                 'loaded_sources': ['CCRD', 'CW', 'PLA', 'RMRB']},
                    'source': {'revision': plan['identity']['source_revision'], 'url': plan['identity']['source_url'],
                               'source_stable': True, **plan['identity']['new_text']}}
        return plan, raw, evidence

    def apply(self, plan, raw, evidence, store, **changes):
        args = {'approved_description': self.description, 'expected_parent': self.pointer, 'expected_etag': '"v1"',
                'store': store, 'immutable_manifest': raw, 'evidence': evidence, **changes}
        return pointer.apply(plan, **args)

    def test_exact_parent_activation_and_candidate_required_before_single_cas(self):
        plan, raw, evidence = self.args(); store = PointerMemory(self.pointer)
        result = self.apply(plan, raw, evidence, store)
        self.assertEqual(result['status'], 'written'); self.assertEqual(store.raw, raw); self.assertEqual(store.writes, 1)
        self.assertFalse(result['external_writers_verified'])
        with self.assertRaisesRegex(ValueError, 'identity or ETag changed'):
            self.apply(plan, raw, evidence, store)
        self.assertEqual(store.writes, 1)

    def test_unverified_or_mismatched_evidence_never_writes(self):
        plan, raw, evidence = self.args()
        variants = ({**evidence, 'consumer': {**evidence['consumer'], 'cw0_bytes_verified': False}},
                    {**evidence, 'consumer': {**evidence['consumer'], 'loaded_sources': ['CCRD', 'CW']}},
                    {**evidence, 'activation': {**evidence['activation'], 'runtime_observed_twice': False}},
                    {**evidence, 'activation': {**evidence['activation'], 'activation': {'path': 'current.json'}}},
                    {**evidence, 'source': {**evidence['source'], 'source_stable': False}},
                    {**evidence, 'manifest_sha256': 'f' * 64}, {**evidence, 'generation': 'wrong'})
        for index, value in enumerate(variants):
            store = PointerMemory(self.pointer)
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'lacks exact'):
                self.apply(plan, raw, value, store)
            self.assertEqual(store.writes, 0)
        store = PointerMemory(self.pointer)
        with self.assertRaisesRegex(ValueError, 'immutable manifest'):
            self.apply(plan, raw + b' ', evidence, store)
        self.assertEqual(store.writes, 0)

    def test_changed_etag_or_same_generation_changed_bytes_fails_closed(self):
        plan, raw, evidence = self.args(); store = PointerMemory(self.pointer)
        store.etag = '"new"'
        with self.assertRaisesRegex(ValueError, 'identity or ETag changed'):
            self.apply(plan, raw, evidence, store)
        self.assertEqual(store.writes, 0)
        store = PointerMemory(self.pointer + b' ')
        with self.assertRaisesRegex(ValueError, 'identity or ETag changed'):
            self.apply(plan, raw, evidence, store)
        self.assertEqual(store.writes, 0)

    def test_lost_write_response_reads_back_once_no_replay(self):
        plan, raw, evidence = self.args(); store = PointerMemory(self.pointer); store.lost = True
        result = self.apply(plan, raw, evidence, store)
        self.assertEqual(result['status'], 'written-response-unknown'); self.assertEqual(store.writes, 1)

    def test_adapter_only_touches_current_and_requires_exact_strong_etag(self):
        backing = Mock(); backing._location.return_value = ('vomebook', 'ccrd-index')
        client = backing._client.return_value
        client.get_object.return_value = {'Body': BytesIO(self.pointer), 'ContentLength': len(self.pointer), 'ETag': '"x"'}
        adapter = pointer.S3Pointer(backing)
        self.assertEqual(adapter.read_for_update(), (self.pointer, '"x"'))
        adapter.compare_and_swap(raw=self.pointer, expected_etag='"x"')
        kwargs = client.put_object.call_args.kwargs
        self.assertEqual(kwargs['Key'], 'current.json'); self.assertEqual(kwargs['IfMatch'], '"x"')
        with self.assertRaises(ValueError): adapter.compare_and_swap(raw=self.pointer, expected_etag='W/"x"')


if __name__ == '__main__':
    unittest.main()
