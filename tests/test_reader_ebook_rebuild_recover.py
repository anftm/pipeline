import json
import unittest
from unittest.mock import patch

from scripts import reader_ebook_rebuild_recover as recovery
from scripts import reader_ebook_rebuild_transport as transport, reader_ebook_recovery_inputs as inputs
from scripts.reader_image_history_store import S3ClaimStore, WriteUnconfirmed
from tests import test_reader_ebook_rebuild_transport as fixtures


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        world = self.world = fixtures.TransportTests()
        self.addCleanup(world.doCleanups)
        world.setUp()
        world.review = json.loads(fixtures.v3.encode(world.review))
        world.review['pages_authority'] = {'code_sha':'a'*40, 'search_generation':'d'*64}
        world.review['version'] = 1
        approval = fixtures.v3.encode(world.review)
        self.approval = fixtures.digest(approval)
        world.s3.data[inputs.REVIEW_PREFIX+self.approval+'.json'] = approval
        self.parents = {'index':world.parent_index, 'sidecar':world.before, 'pointer':world.parent_pointer}
        inputs.retain(S3ClaimStore(world.s3), world.reader, world.review, self.approval, guard=world.guard, **self.parents)
        # The fresh hosted runner reopens this immutable archive, not the live catalog.
        archive = recovery.CatalogParent(world.s3, digest=world.legacy_parent.sha256,
                                        size=world.legacy_parent.size, etag=world.legacy_parent.etag)
        world.s3.data[archive._v1_path] = world.legacy_raw
        world.io.stage(review=world.review, review_sha256=self.approval, bundles={world.key:world.bundle},
                       after_sidecar=world.after)

    def step(self, phase):
        w = self.world
        # Reopen the durable bytes and use a fresh adapter on every invocation.
        fresh = transport.GuardedTransport(w.s3, w.reader, guard=w.guard)
        parents = inputs.load(S3ClaimStore(w.s3), w.reader, w.review, self.approval)
        return recovery.run(fresh, w.review, self.approval, parents, phase)

    def inspect(self, phase):
        w = self.world
        before = list(w.s3.puts)
        self.assertEqual(self.step('inspect')['next_phase'], phase)
        self.assertEqual(w.s3.puts, before)

    def test_start_at_complete_stage_and_resume_exact_phases(self):
        w = self.world
        before = list(w.s3.puts)
        self.inspect('canonical')
        with self.assertRaisesRegex(ValueError, 'requested recovery phase'):
            self.step('v3')
        self.assertEqual(w.s3.puts, before)
        self.step('canonical')
        self.inspect('legacy-start')
        self.step('legacy-start')
        self.inspect('v3')
        self.step('v3')
        self.inspect('pages')
        before = list(w.s3.puts)
        publication = self.step('pages')
        self.assertTrue(publication['applied'])
        self.assertEqual(publication['generation'], w.review['target_pointer']['generation'])
        self.assertEqual(publication['pages_pins'], w.review['pages_authority'])
        self.assertEqual(w.s3.puts, before)
        roots = [key for key, _ in before if key in (fixtures.index.INDEX, fixtures.legacy.CATALOG,
                                                    fixtures.legacy.SIDECAR, fixtures.v3.POINTER)]
        self.assertEqual(roots, [fixtures.index.INDEX, fixtures.legacy.CATALOG, fixtures.legacy.SIDECAR, fixtures.v3.POINTER])

    def test_catalog_response_lost_then_only_sidecar_is_written(self):
        w = self.world
        self.step('canonical')
        w.s3.lose_response = lambda key:key == fixtures.legacy.CATALOG
        with self.assertRaises((WriteUnconfirmed, RuntimeError, TimeoutError)):
            self.step('legacy-start')
        w.s3.lose_response = lambda _:False
        self.inspect('legacy-resume')
        before = len(w.s3.puts)
        self.step('legacy-resume')
        self.assertNotIn(fixtures.legacy.CATALOG, [key for key,_ in w.s3.puts[before:]])
        self.inspect('v3')

    def test_sidecar_response_lost_then_no_root_is_replayed(self):
        w = self.world
        self.step('canonical')
        w.s3.lose_response = lambda key:key == fixtures.legacy.SIDECAR
        with self.assertRaises((WriteUnconfirmed, RuntimeError, TimeoutError)):
            self.step('legacy-start')
        w.s3.lose_response = lambda _:False
        self.inspect('legacy-resume')
        before = len(w.s3.puts)
        self.step('legacy-resume')
        self.assertTrue(all(key.startswith(fixtures.LEGACY_JOURNAL_PREFIX) for key,_ in w.s3.puts[before:]))
        self.inspect('v3')

    def test_spent_canonical_permit_and_unmoved_parent_stops(self):
        w = self.world
        with patch.object(transport.CheckpointAdapter, 'compare_and_swap', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                self.step('canonical')
        before = list(w.s3.puts)
        with self.assertRaisesRegex(ValueError, 'spent but parent is live'):
            self.step('inspect')
        self.assertEqual(w.s3.puts, before)

    def test_canonical_and_pointer_response_loss_are_observed_without_replay(self):
        w = self.world
        w.s3.lose_response = lambda key:key == fixtures.index.INDEX
        try:
            self.step('canonical')
        except (WriteUnconfirmed, RuntimeError, TimeoutError):
            pass  # The adapter may have independently reconciled the response already.
        self.inspect('legacy-start')
        w.s3.lose_response = lambda _:False
        self.step('legacy-start')
        w.s3.lose_response = lambda key:key == fixtures.v3.POINTER
        try:
            self.step('v3')
        except (WriteUnconfirmed, RuntimeError, TimeoutError):
            pass
        self.inspect('pages')
        before = list(w.s3.puts)
        self.step('pages')
        self.assertEqual(before, w.s3.puts)
        for root in (fixtures.index.INDEX, fixtures.v3.POINTER):
            self.assertEqual(sum(path == root for path,_ in before), 1)

    def test_tampered_parent_and_foreign_permit_both_stop(self):
        w = self.world
        path = inputs.PREFIX+self.approval+'/pointer.json'
        orig = w.s3.data[path]
        w.s3.data[path] = orig.strip()
        before = list(w.s3.puts)
        with self.assertRaisesRegex(ValueError, 'SHA-256 differs'):
            self.step('inspect')
        w.s3.data[path] = orig
        w.s3.data[fixtures.index.PERMIT_PREFIX+w.review['canonical_parent_sha256']+'.json'] = b'{}'
        with self.assertRaisesRegex(ValueError, 'foreign canonical permit'):
            self.step('canonical')
        self.assertEqual(w.s3.puts, before)
