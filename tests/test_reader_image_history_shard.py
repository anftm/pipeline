"""No remote I/O: exact shard splice, actual producer merges, and GC graph."""
import hashlib
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path

from scripts import reader_gc_graph as gc, reader_image_history as history
from scripts import reader_image_history_shard as subject
from scripts.reader_stream_checkpoint_cas import Snapshot
from tests import test_reader_image_history as fixture


def snapshot(index, etag):
    raw = index if isinstance(index, bytes) else (json.dumps(index) + '\n').encode()
    return Snapshot(json.loads(raw), etag, hashlib.sha256(raw).hexdigest(), raw)


class ShardSpliceTests(unittest.TestCase):
    def setUp(self):
        f = fixture.ImageHistoryTests()
        f.setUp()
        f.legacy()
        self.key = f.intent.key
        self.old = f.old_row.copy()
        self.old.pop('processing_roots', None)
        self.new = f.new_row.copy()
        self.new.pop('processing_roots', None)
        address = {'version': 1, 'source_sha256': self.old['source_sha256'],
                   'files': {'document.avif': self.new['sha256']}}
        root = 'pages/image/png/' + history._sha(json.dumps(address, sort_keys=True, separators=(',', ':')).encode())
        self.new.update(profile=history.AVIF_PROFILE, root=root, path=root + '/document.avif')
        self.number = subject.shard_for(self.key, 8)
        self.path = f'pages/image/index-v2-{self.number:02d}.json'
        bucket = history.BUCKET
        other_keys = [f'library/images\0other-{i}.png' for i in range(300)
                      if subject.shard_for(f'library/images\0other-{i}.png', 8) == self.number]
        other = {'key': other_keys[0], 'bucket': bucket, 'path': 'pages/image/png/' + 'e' * 64 + '/document.webp'}
        self.shard_value = {'version': 1, 'kind': 'image-stream-index', 'files': [self.old, other],
                            'failures': [{'key': other_keys[1], 'error': 'keep'}], 'metadata': {'keep': True}}
        self.shard = snapshot(self.shard_value, '"shard-parent"')
        self.canonical = snapshot({'version': 1, 'kind': 'image-stream-index', 'files': [self.new, other],
                                   'checkpoint_operation': fixture.OP}, '"canonical-target"')
        self.pin = subject.ShardPin(self.path, self.shard.raw_sha256, len(self.shard.raw), self.shard.etag)
        self.plan = self.splice()

    def splice(self, **changes):
        return subject.splice(**({'key': self.key, 'old_row_sha256': history.row_digest(self.old),
                                  'canonical_target': self.canonical.raw, 'canonical_sha256': self.canonical.raw_sha256,
                                  'shard': self.shard, 'pin': self.pin} | changes))

    def test_exact_splice_preserves_every_other_row_failure_and_field(self):
        expected = {**self.shard_value, 'files': [self.new, self.shard_value['files'][1]],
                    'checkpoint_operation': fixture.OP}
        self.assertEqual(self.plan.target, history._encode(expected))
        self.assertEqual(self.plan.parent, self.pin)
        self.assertEqual(self.plan.canonical_sha256, self.canonical.raw_sha256)

    def test_mismatched_parent_row_etag_or_canonical_identity_fail_closed(self):
        for fields in ({'pin': replace(self.pin, path=history.CANONICAL)},
                       {'pin': replace(self.pin, bytes=self.pin.bytes + 1)},
                       {'pin': replace(self.pin, etag='"rotated"')},
                       {'pin': replace(self.pin, sha256='f' * 64)},
                       {'old_row_sha256': 'f' * 64}, {'canonical_sha256': 'f' * 64}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.splice(**fields)
        for change in ({'profile': None}, {'source_bytes': self.new['source_bytes'] + 1},
                       {'source_sha256': 'f' * 64}, {'root': self.old['path'].rsplit('/', 1)[0]}):
            bad = snapshot({**self.canonical.value, 'files': [{**self.new, **change}]}, '"bad-target"')
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.splice(canonical_target=bad.raw, canonical_sha256=bad.raw_sha256)

    def test_bad_readback_is_not_observed_even_if_selected_row_matches(self):
        altered = snapshot({**json.loads(self.plan.target), 'unrelated': True}, '"different"')
        for shard in (altered, replace(self.shard, etag='"rotated"'), snapshot(self.plan.target, self.shard.etag)):
            with self.subTest(etag=shard.etag), self.assertRaisesRegex(ValueError, 'neither the exact parent'):
                subject.prove_observed(self.plan, canonical=self.canonical, shard=shard,
                                       producer_checkout=Path('/not-read'))

    def test_physical_shard_pins_old_webp_after_canonical_only_switch(self):
        assets = {history.CANONICAL: self.canonical.raw, self.path: self.shard.raw,
                  self.old['path']: b'webp', self.new['path']: b'avif', self.shard_value['files'][1]['path']: b'other',
                  'reader-index/reader_assets.json.gz': b'{"v":1,"f":{}}'}

        class Inventory:
            def list_files(self, bucket, prefixes):
                return {p for p in assets if p.startswith(prefixes)} if bucket == history.BUCKET else set()

            def read_bytes(self, bucket, path):
                return assets[path]

        self.assertTrue(gc.is_root(self.path, history.BUCKET))
        for raw, reachable in ((self.shard.raw, True), (self.plan.target, False)):
            assets[self.path] = raw
            graph = gc.ReferenceGraph(Inventory())
            report = graph.build()
            self.assertTrue(report['graph_complete'], report['blockers'])
            ref = history.BUCKET, self.old['path']
            self.assertEqual(ref in graph.references, reachable)
            self.assertEqual(self.old['path'] in report['buckets'][history.BUCKET]['candidates'], not reachable)
            self.assertFalse(report['deletion_enabled'])

    @unittest.skipUnless(os.environ.get('READER_IMAGE_PRODUCER_CHECKOUT'), 'requires exact reviewed producer checkout')
    def test_actual_revision_preserves_avif_with_old_shard_and_with_splice(self):
        checkout = Path(os.environ['READER_IMAGE_PRODUCER_CHECKOUT'])
        for shard, state in ((self.shard, 'parent'), (snapshot(self.plan.target, '"spliced"'), 'spliced')):
            observed = subject.prove_observed(self.plan, canonical=self.canonical, shard=shard, producer_checkout=checkout)
            self.assertEqual(observed['shard_state'], state)
            self.assertEqual(observed['selected_row_sha256'], history.row_digest(self.new))
            self.assertEqual(observed['producer_revision'], subject.PRODUCER)
            self.assertFalse(observed['deletion_authorized'])
        # Even a newer ordinary WebP checkpoint must not beat identical AVIF
        # source bytes. This establishes more than a timestamp tie-break.
        newer = snapshot({**self.shard_value, 'files': [{**self.old, 'checkpointed_at': '2030-01-01T00:00:00Z'},
                                                        self.shard_value['files'][1]]}, '"newer-webp"')
        pin = replace(self.pin, sha256=newer.raw_sha256, bytes=len(newer.raw), etag=newer.etag)
        newer_plan = self.splice(shard=newer, pin=pin, old_row_sha256=history.row_digest(newer.value['files'][0]))
        observed = subject.prove_observed(newer_plan, canonical=self.canonical, shard=newer, producer_checkout=checkout)
        self.assertEqual(observed['shard_state'], 'parent')


if __name__ == '__main__':
    unittest.main()
