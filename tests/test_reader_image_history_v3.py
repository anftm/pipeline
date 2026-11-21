"""A lost Reader v3 attempt can only be observed, not repeated."""
import unittest
from unittest.mock import patch

from scripts import reader_image_history_v3 as gate
from scripts.reader_image_history_store import WriteUnconfirmed
from tests.test_reader_image_history_store import MemoryS3


class TestPromotionGate(unittest.TestCase):
    def setUp(self):
        self.client = MemoryS3()
        def pointer(generation):
            return dict(version=1, kind='reader-reading-pointer', generation=generation,
                        catalog={'path': f'reader-index/v3/generations/{generation}/catalog.json'})
        self.old = pointer('a' * 64)
        self.new = pointer('b' * 64)
        self.raw = gate.v3.encode(self.old)
        self.active = self.old
        self.canonical = b'candidate sidecar'
        self.writes = 0
        self.fault = ''
        self.preview = self.new

    def read_bytes(self, bucket, path):
        self.assertEqual(bucket, gate.v3.ASSETS)
        return gate.v3.encode(self.active) if path == gate.v3.POINTER else self.canonical

    def unify(self, store, *, apply, **options):
        self.assertIs(store, self)
        self.assertEqual(options, dict(expected_pointer=self.old, expected_canonical=gate.history._sha(self.canonical),
                                       conditional_pointer=True))
        if apply:
            self.writes += 1
            if self.fault == 'before':
                raise TimeoutError('lost request')
            self.active = self.new
            if self.fault == 'after':
                raise TimeoutError('lost response')
        return {'pointer': self.preview}

    def run_gate(self, **kwargs):
        args = dict(store=self, client=self.client, parent_raw=self.raw, target_pointer=self.new,
                    canonical_sha256=gate.history._sha(self.canonical), legacy_seal='f' * 64, guard=lambda: True)
        with patch.object(gate.v3, 'current', side_effect=lambda _: (self.active, {'parent_generation': self.old['generation']})), \
                patch.object(gate.v3, 'unify', side_effect=self.unify):
            return gate.promote(**(args | kwargs))

    def test_lost_acknowledgement_is_observed(self):
        self.fault = 'after'
        with self.assertRaises(TimeoutError):
            self.run_gate()
        r = self.run_gate(observe_only=True)
        self.assertEqual(r['pointer'], self.new)
        self.assertFalse(r['pages_dispatched'])
        self.assertEqual((self.writes, len(self.client.puts)), (1, 1))

    def test_spent_request_with_parent_does_not_replay(self):
        self.fault = 'before'
        with self.assertRaises(TimeoutError):
            self.run_gate()
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_gate()
        self.assertEqual((self.writes, len(self.client.puts)), (1, 1))

    def test_new_permit_is_not_won_when_acknowledgement_is_ambiguous(self):
        self.client.fault = 'after'
        with self.assertRaises(WriteUnconfirmed):
            self.run_gate()
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_gate(observe_only=True)
        self.assertEqual((self.writes, len(self.client.puts)), (0, 1))

    def test_wrong_successor_stops_before_permit(self):
        self.preview = self.old
        with self.assertRaisesRegex(ValueError, 'reviewed pointer'):
            self.run_gate()
        self.assertEqual((self.writes, len(self.client.puts)), (0, 0))
