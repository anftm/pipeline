"""Protected staging simulation never invokes production storage adapters."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts import reader_epub_shadow as shadow
from scripts import reader_epub_stage_simulation as subject
from tests.test_reader_epub_shadow import epub, plan

WHEN = '2026-11-09T00:00:00Z'


class EpubStageSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'shadow'
        raw = epub(); self.plan = plan(raw)
        self.report = shadow.build(self.plan, self.output, fetch=lambda _u, path, _t: path.write_bytes(raw))
        self.preview = shadow.staging_plan(self.plan, self.report, self.output)
        self.action = self.preview['actions'][0]

    def test_all_protections_precede_content_and_resume_is_idempotent(self):
        store = subject.MemoryStore()
        result = subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(result['protected_sources'], 1)
        self.assertEqual(result['verified_objects'], len(self.action['files']))
        self.assertEqual(result['remote_writes'], 0)
        self.assertFalse(result['index_written'])
        self.assertFalse(result['publication_ready'])
        writes = [event for event in store.events if event[0] == 'create']
        self.assertEqual(writes[0][1], 'protection')
        self.assertTrue(all(event[1] == 'object' for event in writes[1:]))
        before = len(writes)
        again = subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(again['simulated_new_creates'], 0)
        self.assertEqual(len([event for event in store.events if event[0] == 'create']), before)
        record = json.loads(store.records[self.action['protection_path']])
        self.assertEqual(record['status'], 'processing')
        self.assertEqual(record['release_policy'], 'explicit-publisher-handoff')

    def test_lost_response_for_protection_and_object_is_read_back_not_replayed(self):
        first_object = self.action['files'][0]['remote']
        store = subject.MemoryStore(lose_once={('protection', self.action['protection_path']), ('object', first_object)})
        result = subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(result['verified_objects'], len(self.action['files']))
        for kind, key in [('protection', self.action['protection_path']), ('object', first_object)]:
            self.assertEqual(store.events.count(('create', kind, key)), 1)
            self.assertGreaterEqual(store.events.count(('inspect', kind, key)), 2)

    def test_failure_before_protection_blocks_all_objects_and_explicit_resume(self):
        ident = ('protection', self.action['protection_path'])
        store = subject.MemoryStore(fail_before={ident})
        with self.assertRaises(TimeoutError):
            subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(store.objects, {})
        self.assertEqual(store.events.count(('create', *ident)), 1)
        done = subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(done['verified_objects'], len(self.action['files']))
        self.assertEqual(store.events.count(('create', *ident)), 2)

    def test_foreign_protection_or_immutable_object_fails_before_replacement(self):
        store = subject.MemoryStore()
        store.records[self.action['protection_path']] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'protection differs'):
            subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(store.objects, {})
        store = subject.MemoryStore()
        target = self.action['files'][0]['remote'];store.objects[target] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'object differs'):
            subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(store.objects[target], b'foreign')
        self.assertNotIn(('create', 'object', target), store.events)

    def test_multiple_protections_all_precede_first_object(self):
        raw = epub();p = plan(raw);base = p['sources'][0]
        # Search deterministically for a second independent key in the same shard.
        from scripts import reader_stream_index, search_snapshot
        import hashlib
        for number in range(1, 100):
            key = 'VoiceOfML/Test\0other' + str(number) + '.epub'
            if reader_stream_index.shard_for(key, 8) == p['shard']:
                break
        second = {**base, 'key': key, 'path': key.split('\0', 1)[1],
                  'previous_manifest': 'chapters/ebook/epub/other/chapter-manifest.json'}
        p['sources'].append(second)
        p['plan_sha256'] = hashlib.sha256(search_snapshot.canonical({k: v for k, v in p.items() if k != 'plan_sha256'})).hexdigest()
        out = Path(self.temp.name) / 'multiple'
        # Distinct source bytes are needed because identical content intentionally
        # maps to the same immutable root and the preview rejects duplicates.
        import io, zipfile
        extra = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(extra, 'w') as target:
            for name in source.namelist():
                value = source.read(name)
                if name == 'chapter.xhtml': value = value.replace(b'New content', b'Other text!')
                target.writestr(name, value)
        other = extra.getvalue()
        second['current_source_sha256'] = hashlib.sha256(other).hexdigest()
        p['plan_sha256'] = hashlib.sha256(search_snapshot.canonical({k: v for k, v in p.items() if k != 'plan_sha256'})).hexdigest()
        def fetch(url, path, _token): path.write_bytes(other if 'other' in url else raw)
        report = shadow.build(p, out, fetch=fetch)
        store = subject.MemoryStore(); result = subject.simulate(p, report, out, store, created_at=WHEN)
        creates = [event for event in store.events if event[0] == 'create']
        self.assertEqual([event[1] for event in creates[:2]], ['protection', 'protection'])
        self.assertEqual(result['protected_sources'], 2)
        self.assertTrue(all(event[1] == 'object' for event in creates[2:]))

    def test_local_tamper_or_production_adapter_is_rejected(self):
        with self.assertRaisesRegex(TypeError, 'memory store'):
            subject.simulate(self.plan, self.report, self.output, object(), created_at=WHEN)
        file = self.output / '0' / 'bundle' / self.action['files'][0]['path']
        file.write_bytes(file.read_bytes() + b'changed')
        store = subject.MemoryStore()
        with self.assertRaises(ValueError): subject.simulate(self.plan, self.report, self.output, store, created_at=WHEN)
        self.assertEqual(store.events, [])


if __name__ == '__main__': unittest.main()
