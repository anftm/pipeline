"""Immutable CCRD staging cannot touch current pointer or infer activation."""
import copy
import gzip
import hashlib
import unittest
from unittest.mock import patch

from scripts import ccrd_lifecycle
from scripts import ccrd_stage_objects as staging
from scripts import ccrd_staging_plan as plan
from tests.test_ccrd_candidate_assembly import CandidateAssemblyTests


class MemoryStore:
    def __init__(self, pointer):
        self.pointer = pointer
        self.objects = {}
        self.creates = 0
        self.lose_response = False
        self.move_after = False

    def parent_pointer(self):
        if self.move_after and self.creates: return self.pointer + b' '
        return self.pointer

    def inspect(self, key):
        return self.objects.get(key)

    def create(self, key, _path, expected):
        self.creates += 1
        if key in self.objects: raise RuntimeError('already exists')
        self.objects[key] = dict(expected)
        if self.lose_response: raise TimeoutError('lost response')


class StageObjectTests(CandidateAssemblyTests):
    def prepare(self):
        with patch.object(plan.assembly, '_cw_database',
                          return_value=({'sha256': staging._identity(self.candidate)['sha256'],
                                         'bytes': self.candidate.stat().st_size}, {'seconds': 0.1})):
            description = self.make()
        value = plan.make(self.pointer, description)
        self.description = description
        for ref in value['objects']:
            if ref['kind'] != 'archive': continue
            old_prefix = 'generations/' + self.manifest['generation'] + '/'
            path = self.parent / ref['source_path'][len(old_prefix):]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(b'checked complete year archive', mtime=0))
        return value

    def stage(self, value, store):
        return staging.stage(value, approved_description=self.description, parent_root=self.parent,
                             candidate_root=self.output, candidate_cw=self.candidate, store=store)

    def test_all_verified_objects_are_immutable_and_repeat_is_read_only(self):
        value = self.prepare(); store = MemoryStore(self.pointer)
        result = self.stage(value, store)
        self.assertEqual(result['objects_verified'], len(value['objects']))
        self.assertEqual(result['objects_created'], len(value['objects']))
        self.assertFalse(result['manifest_written']); self.assertFalse(result['pointer_written'])
        self.assertFalse(result['publication_ready']); self.assertFalse(result['external_writers_verified'])
        before = store.creates
        self.assertEqual(self.stage(value, store)['objects_created'], 0)
        self.assertEqual(store.creates, before)

    def test_s3_adapter_verifies_stream_bytes_and_uses_conditional_create(self):
        from io import BytesIO
        from unittest.mock import Mock
        generation = 'ccrd-four-' + 'a' * 64
        backing = Mock(); backing._location.return_value = ('vomebook', 'ccrd-index')
        client = backing._client.return_value
        adapter = staging.S3Store(generation=generation, store=backing)
        key = f'generations/{generation}/data/example.json'
        path = self.root / 'object'; path.write_bytes(b'abc')
        expected = staging._identity(path)
        client.get_object.return_value = {'Body': BytesIO(b'abc'), 'ContentLength': 3}
        self.assertEqual(adapter.inspect(key), expected)
        client.get_object.return_value = {'Body': BytesIO(b'ab'), 'ContentLength': 3}
        with self.assertRaisesRegex(ValueError, 'truncated'): adapter.inspect(key)
        adapter.create(key, path, expected)
        kwargs = client.put_object.call_args.kwargs
        self.assertEqual(kwargs['Key'], key); self.assertEqual(kwargs['IfNoneMatch'], '*')
        self.assertEqual(kwargs['ContentLength'], 3)

    def test_lost_create_response_is_read_back_without_replay(self):
        value = self.prepare(); store = MemoryStore(self.pointer); store.lose_response = True
        result = self.stage(value, store)
        self.assertEqual(result['objects_created'], len(value['objects']))
        self.assertEqual(store.creates, len(value['objects']))

    def test_foreign_existing_bytes_and_moving_parent_fail_closed(self):
        value = self.prepare(); store = MemoryStore(self.pointer)
        first = value['objects'][0]; store.objects[first['target']] = {'sha256': 'f' * 64, 'bytes': first['bytes']}
        with self.assertRaisesRegex(ValueError, 'readback differs'): self.stage(value, store)
        store = MemoryStore(self.pointer); store.move_after = True
        with self.assertRaisesRegex(ValueError, 'changed while staging'): self.stage(value, store)
        self.assertGreater(store.creates, 0)

    def test_unapproved_target_source_or_changed_local_bytes_never_stage(self):
        value = self.prepare(); store = MemoryStore(self.pointer)
        broken = copy.deepcopy(value); broken['objects'][0]['target'] = 'current.json'
        with self.assertRaises(ValueError): self.stage(broken, store)
        broken = copy.deepcopy(value); broken['objects'][0]['source_path'] = 'control/other.json'
        with self.assertRaises(ValueError): self.stage(broken, store)
        self.candidate.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'local file differs'): self.stage(value, store)
        self.assertEqual(store.creates, 0)

    def test_parent_and_candidate_must_reproduce_the_exact_plan_before_any_upload(self):
        value = self.prepare(); store = MemoryStore(self.pointer + b' ')
        with self.assertRaisesRegex(ValueError, 'parent pointer identity'):
            self.stage(value, store)
        self.assertEqual(store.creates, 0)
        store = MemoryStore(self.pointer)
        broken = copy.deepcopy(value); broken['manifest']['unexpected'] = 'foreign'
        broken['manifest_sha256'] = hashlib.sha256(ccrd_lifecycle.canonical(broken['manifest'])).hexdigest()
        with self.assertRaisesRegex(ValueError, 'differs from the approved'):
            self.stage(broken, store)
        self.assertEqual(store.creates, 0)

    def test_store_refuses_any_other_bucket_or_mutable_root(self):
        generation = 'ccrd-four-' + 'a' * 64
        s = staging.S3Store(generation=generation, store=object())
        for key in ('current.json', f'generations/{generation}/manifest.json', 'control/anything'):
            with self.subTest(key=key), self.assertRaises(ValueError): s._location(key)


if __name__ == '__main__':
    unittest.main()
