import base64
import copy
import gzip
import json
import unittest
from unittest.mock import patch

from scripts import reader_page_history_publish as publish
from scripts import reader_page_history_inventory as inventory
from scripts import reader_page_history_replacement as replacement
from scripts import publish_reader_v3 as publication, reader_page_audit as audit
from scripts import reader_page_migrate as migration, reader_page_redirects as redirects
from scripts import reader_page_redirect_retirement as finalization
from tests import test_reader_page_history_replacement as fixtures
from tests.test_reader_page_audit import KEY, OLD, NEW


class MemoryStore:
    """Only the proposed narrow write interface; no generic put or delete."""
    def __init__(self, source):
        self.objects = copy.deepcopy(source.objects)
        self.api = source.api
        self.writes = []
        self.cas_attempts = 0
        self.mode = 'success'
        self.stage_hook = None
        self.revision = 0
        self.writer = True

    def read_bytes(self, bucket, path):
        try:
            return self.objects[bucket, path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def list_files(self, bucket, prefixes):
        return [path for b, path in self.objects if b == bucket and path.startswith(prefixes)]

    def assert_serialized_writer(self, protocol):
        if not self.writer:
            raise ValueError('writer window missing')

    def read_for_update(self, bucket, path):
        assert (bucket, path) == (audit.ASSETS, publish.RETIREMENT)
        if self.cas_attempts and self.mode == 'unreadable':
            raise OSError('readback unavailable')
        raw = self.objects.get((bucket, path))
        return raw, None if raw is None else (finalization._sha(raw) + ':' + str(self.revision))

    def immutable_put(self, bucket, path, raw):
        assert bucket == audit.ASSETS
        assert path.startswith(('reader-index/snapshots/', replacement.PREFIX, publish.INTENTS))
        previous = self.objects.get((bucket, path))
        if previous is not None and previous != raw:
            raise ValueError('immutable conflict')
        self.objects[bucket, path] = raw
        self.writes.append((bucket, path))
        if self.stage_hook:
            self.stage_hook(path)

    def put_if_match(self, bucket, path, raw, token):
        assert (bucket, path) == (audit.ASSETS, publish.RETIREMENT)
        self.cas_attempts += 1
        if self.mode == 'conflict':
            self.objects[bucket, path] = b'{"foreign":true}'
            raise RuntimeError('CAS conflict')
        if self.mode == 'unknown':
            raise OSError('response lost, outcome unknown')
        if self.read_for_update(bucket, path)[1] != token:
            raise RuntimeError('CAS conflict')
        self.objects[bucket, path] = raw
        self.writes.append((bucket, path))
        self.revision += 1
        if self.mode == 'lost-response':
            raise OSError('committed but response lost')


class HistoryPublishTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HistoryReplacementTests()
        self.fixture.setUp()
        self.store = MemoryStore(self.fixture.store)
        self.fixture.store = self.store
        self.old, self.old_promotion = self.fixture.history()
        clean, _ = redirects.rewrite(self.fixture.base, {OLD: NEW})
        self.bind_canonical(clean)
        self.install_current(clean)
        self.observations = 0

    def bind_canonical(self, base):
        packed = gzip.compress(audit.encode(base), mtime=0)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = packed
        legacy, generation = publication.reader_lifecycle.catalog_generation(
            base, now='2026-11-15T00:00:00+00:00')
        ref = publication.metadata(audit.ASSETS,
            f'reader-index/generations/{generation}/reader_assets.json.gz', packed)
        legacy['generations'][generation]['snapshot'] = ref
        self.store.objects[audit.ASSETS, ref['path']] = packed
        self.store.objects[audit.ASSETS, migration.CATALOG] = publication.encode(legacy)
        return legacy, ref

    def install_current(self, base, **fields):
        self.current, promotion = self.fixture.history(base, metadata={'finalized': True}, **fields)
        self.generation = self.current['generation']
        self.pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': self.generation,
                        'catalog': promotion['resources'][0], 'resources': promotion['resources']}
        self.store.objects[audit.ASSETS, publication.POINTER] = publication.encode(self.pointer)
        self.receipts = {surface: {'version': 1, 'surface': surface, 'active': True,
            'generation': self.generation, 'catalog_sha256': self.pointer['catalog']['sha256'],
            'files': 0, 'projection_verified': True, 'checked_url': 'https://fixture/' + surface,
            'checked_at': '2026-11-15T00:00:00+00:00'} for surface in ('hf', 'pages')}
        promotion.update(acks=copy.deepcopy(publish.ACKED), receipts=copy.deepcopy(self.receipts))
        self.current_promotion = replacement.PROMOTIONS + self.generation + '.json'
        self.store.objects[audit.ASSETS, self.current_promotion] = publication.encode(promotion)

    def observe(self, store, generation):
        self.assertIs(store, self.store)
        self.assertEqual(generation, self.generation)
        self.observations += 1
        return {surface: {**receipt, 'checked_at': str(self.observations)}
                for surface, receipt in self.receipts.items()}

    def prepare(self):
        return publish.prepare(self.store, {OLD: NEW},
            current_ack_verified_generation=self.generation, observe_consumers=self.observe)

    def apply(self, plan):
        return publish.apply(self.store, plan,
            current_ack_verified_generation=self.generation, observe_consumers=self.observe)

    def state(self, **fields):
        value = {'version': 1, 'kind': 'reader-v3-retirement', 'resources': [],
                 'retired_paths': [], 'rollback_replacements': {}, **fields}
        self.store.objects[audit.ASSETS, publish.RETIREMENT] = publication.encode(value)
        return value

    def assert_originals(self, before):
        for key, raw in before.items():
            if key != (audit.ASSETS, publish.RETIREMENT):
                self.assertEqual(self.store.objects[key], raw, key)

    def test_fresh_root_read_only_plan_and_single_cas_with_exact_audit(self):
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        self.assertIsNone(plan['before'])
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.writes, [])
        self.assertTrue(plan['apply_allowed'])
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'applied')
        self.assertEqual(self.store.cas_attempts, 1)
        self.assertEqual(self.observations, 2)
        self.assert_originals(before)
        after = json.loads(self.store.read_bytes(audit.ASSETS, publish.RETIREMENT))
        item = plan['replacement_plan']['replacements'][0]
        self.assertEqual(after['rollback_replacements'], {self.old['generation']: item['catalog_ref']})
        self.assertEqual(set(after['retired_paths']), {item['old_catalog']['path'], item['old_promotion']['path']})
        self.assertIn(self.pointer['catalog'], after['resources'])
        self.assertIn(item['catalog_ref'], after['resources'])
        raw = self.store.read_bytes(audit.ASSETS, result['intent_ref']['path'])
        intent = json.loads(raw)
        self.assertEqual(finalization._sha(raw), result['intent_ref']['sha256'])
        self.assertEqual(intent['before'], None)
        self.assertEqual(base64.b64decode(intent['after']['base64']), plan['after'])
        self.assertEqual(intent['reviewed_plan']['replacement_plan']['evidence_resources'],
                         plan['replacement_plan']['evidence_resources'])
        self.assertNotEqual(intent['fresh_consumer_observations'], plan['consumer_observations'])
        self.assertEqual(json.loads(before[audit.ASSETS, item['old_promotion']['path']])['acks'],
                         {'hf': False, 'pages': False})
        for flag in ('graph_complete', 'fully_unreferenced', 'consumer_acks_created',
                     'canonical_indexes_changed', 'objects_deleted', 'deletion_authorized'):
            self.assertFalse(result[flag])

    def test_caller_plan_mutation_during_callbacks_cannot_contradict_intent(self):
        for phase in ('writer', 'stage', 'observation'):
            with self.subTest(phase=phase):
                self.setUp()
                plan = self.prepare()
                reviewed = copy.deepcopy(plan)
                def mutate():
                    plan['after'] = b'{"foreign":true}'
                    plan['replacement_plan']['replacements'][0]['catalog_raw'] = b'foreign'
                    plan['replacement_plan']['mapping'].clear()
                    plan['consumer_observations']['hf']['generation'] = 'a' * 64
                if phase == 'writer':
                    self.store.assert_serialized_writer = lambda protocol: mutate()
                elif phase == 'stage':
                    self.store.stage_hook = lambda path: mutate()
                def observe(store, generation):
                    if phase == 'observation':
                        mutate()
                    return self.observe(store, generation)
                result = publish.apply(self.store, plan,
                    current_ack_verified_generation=self.generation, observe_consumers=observe)
                self.assertTrue(result['applied'])
                intent = json.loads(self.store.read_bytes(audit.ASSETS, result['intent_ref']['path']))
                self.assertEqual(intent['reviewed_plan'], publish._jsonable(reviewed))
                self.assertEqual(base64.b64decode(intent['after']['base64']), reviewed['after'])
                self.assertEqual(intent['after'], intent['reviewed_plan']['after'])
                self.assertEqual(self.store.objects[audit.ASSETS, publish.RETIREMENT], reviewed['after'])
                self.assertEqual(self.store.cas_attempts, 1)
                for item in reviewed['replacement_plan']['replacements']:
                    if item['candidate_required']:
                        self.assertEqual(self.store.read_bytes(audit.ASSETS, item['catalog_ref']['path']),
                                         item['catalog_raw'])

    def test_invalid_review_observations_reject_before_staging(self):
        for mode in ('missing', 'partial', 'wrong-generation', 'wrong-digest', 'unverified'):
            with self.subTest(mode=mode):
                self.setUp()
                plan = self.prepare()
                if mode == 'missing':
                    del plan['consumer_observations']
                elif mode == 'partial':
                    del plan['consumer_observations']['pages']
                else:
                    receipt = plan['consumer_observations']['hf']
                    field, value = {'wrong-generation': ('generation', self.old['generation']),
                                    'wrong-digest': ('catalog_sha256', 'a' * 64),
                                    'unverified': ('projection_verified', False)}[mode]
                    receipt[field] = value
                before = copy.deepcopy(self.store.objects)
                with self.assertRaises(ValueError):
                    self.apply(plan)
                self.assertEqual(self.store.objects, before)
                self.assertEqual(self.store.writes, [])
                self.assertEqual(self.store.cas_attempts, 0)

    def test_completed_finalization_uses_suppression_aware_full_equivalence(self):
        canonical = audit.decode_sidecar(self.store.objects[audit.ASSETS, audit.SIDECAR])
        canonical['metadata'] = {'edition': 7}
        suppressed = 'VoiceOfML/Test\0withdrawn.chm'
        canonical['f'][suppressed] = {'s': 2, 'b': audit.ASSETS, 'p': 'chm/withdrawn/index.html'}
        _, ref = self.bind_canonical(canonical)
        pinned = copy.deepcopy(canonical)
        del pinned['f'][suppressed]
        self.install_current(pinned, suppressed_sources=[suppressed])
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        self.assertEqual(plan['finalization_evidence_resources'], [ref])
        self.assertTrue(self.apply(plan)['applied'])
        self.assert_originals(before)

    def test_canonical_pinned_mismatch_rejects_before_staging(self):
        for mode in ('empty', 'missing-route', 'extra-route', 'different-route', 'metadata'):
            with self.subTest(mode=mode):
                self.setUp()
                base = audit.decode_sidecar(self.store.objects[audit.ASSETS, audit.SIDECAR])
                if mode == 'empty':
                    base = {'v': 1, 'f': {}}
                elif mode == 'missing-route':
                    del base['f'][KEY]
                elif mode == 'extra-route':
                    base['f']['VoiceOfML/Test\0extra.epub'] = {
                        's': 2, 'b': audit.ASSETS, 'p': 'epub/extra/index.html'}
                elif mode == 'different-route':
                    base['f'][KEY]['p'] = 'epub/other/index.html'
                    base['f'][KEY]['b'] = audit.ASSETS
                else:
                    base['metadata'] = {'unexpected': True}
                # Keep the legacy record internally valid to isolate full-base
                # equivalence from the independently checked legacy binding.
                self.bind_canonical(base)
                before = copy.deepcopy(self.store.objects)
                with self.assertRaisesRegex(ValueError, 'canonical sidecar differs from full pinned base'):
                    self.prepare()
                self.assertEqual(self.store.objects, before)
                self.assertEqual(self.store.writes, [])

    def test_current_legacy_binding_mismatches_reject_before_staging(self):
        for mode in ('missing-current', 'missing-entry', 'generation', 'status', 'digest',
                     'coverage', 'wrong-bucket', 'pending', 'snapshot-path', 'snapshot-bucket',
                     'snapshot-sha', 'snapshot-size', 'snapshot-missing', 'snapshot-base'):
            with self.subTest(mode=mode):
                self.setUp()
                legacy = json.loads(self.store.objects[audit.ASSETS, migration.CATALOG])
                generation = legacy['current']['generation']
                entry = legacy['generations'][generation]
                ref = entry['snapshot']
                if mode == 'missing-current':
                    legacy['current'] = None
                elif mode == 'missing-entry':
                    del legacy['generations'][generation]
                elif mode == 'generation':
                    entry['generation'] = 'b' * 32
                elif mode == 'status':
                    entry['status'] = 'superseded'
                elif mode == 'digest':
                    entry['sidecar_sha256'] = 'b' * 64
                elif mode == 'coverage':
                    entry['references'] = []
                elif mode == 'wrong-bucket':
                    entry['references'] = [{'bucket': audit.ASSETS, 'path': NEW}]
                elif mode == 'pending':
                    legacy['redirect_sidecar_pending'] = {}
                elif mode == 'snapshot-path':
                    ref['path'] = 'reader-index/generations/wrong/reader_assets.json.gz'
                elif mode == 'snapshot-bucket':
                    ref['bucket'] = audit.BUCKET
                elif mode == 'snapshot-sha':
                    ref['sha256'] = 'b' * 64
                elif mode == 'snapshot-size':
                    ref['bytes'] += 1
                elif mode == 'snapshot-missing':
                    del self.store.objects[audit.ASSETS, ref['path']]
                else:
                    packed = gzip.compress(audit.encode({'v': 1, 'f': {}}), mtime=0)
                    self.store.objects[audit.ASSETS, ref['path']] = packed
                    entry['snapshot'] = publication.metadata(audit.ASSETS, ref['path'], packed)
                self.store.objects[audit.ASSETS, migration.CATALOG] = publication.encode(legacy)
                before = copy.deepcopy(self.store.objects)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.prepare()
                self.assertEqual(self.store.objects, before)
                self.assertEqual(self.store.writes, [])
                self.assertEqual(self.store.cas_attempts, 0)

    def test_legacy_snapshot_drift_is_bound_to_reviewed_plan(self):
        for phase in ('before-apply', 'stage'):
            with self.subTest(phase=phase):
                self.setUp()
                plan = self.prepare()
                ref = plan['finalization_evidence_resources'][0]
                def change():
                    raw = self.store.objects[audit.ASSETS, ref['path']]
                    # Same decoded base, different bytes: exact evidence matters.
                    self.store.objects[audit.ASSETS, ref['path']] = gzip.compress(gzip.decompress(raw), mtime=1)
                if phase == 'before-apply':
                    change()
                else:
                    self.store.stage_hook = lambda path: change()
                with self.assertRaisesRegex(ValueError, 'snapshot checksum mismatch'):
                    self.apply(plan)
                self.assertEqual(self.store.cas_attempts, 0)
                self.assertNotIn((audit.ASSETS, publish.RETIREMENT), self.store.objects)
                if phase == 'before-apply':
                    self.assertEqual(self.store.writes, [])

    def test_optional_legacy_snapshot_matches_finalizer_contract(self):
        legacy = json.loads(self.store.objects[audit.ASSETS, migration.CATALOG])
        del legacy['generations'][legacy['current']['generation']]['snapshot']
        self.store.objects[audit.ASSETS, migration.CATALOG] = publication.encode(legacy)
        plan = self.prepare()
        self.assertEqual(plan['finalization_evidence_resources'], [])
        self.assertTrue(self.apply(plan)['applied'])

    def test_existing_state_preserves_other_pins_and_metadata_replaces_old_resource(self):
        clean, _ = redirects.rewrite(self.fixture.base, {OLD: NEW})
        _, other = self.fixture.history(clean, metadata={'unrelated': 1})
        other_ref = other['resources'][0]
        old_ref = self.old_promotion['resources'][0]
        state = self.state(resources=[other_ref, old_ref], retired_paths=['already/retired'],
                           retired_inputs={'keep': {'bytes': 42}}, retired_input_roots={'keep': {'pages': 3}},
                           unknown_metadata={'preserve': [1, 2]}, replacement_acks=publish.ACKED)
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        result = self.apply(plan)
        self.assertTrue(result['applied'])
        after = json.loads(plan['after'])
        self.assertIn(other_ref, after['resources'])
        self.assertNotIn(old_ref, after['resources'])
        self.assertIn('already/retired', after['retired_paths'])
        for key in ('retired_inputs', 'retired_input_roots', 'unknown_metadata'):
            self.assertEqual(after[key], state[key])
        self.assert_originals(before)
        intent = json.loads(self.store.read_bytes(audit.ASSETS, result['intent_ref']['path']))
        self.assertEqual(base64.b64decode(intent['before']['base64']), plan['before'])

    def test_chm_alias_points_to_transformed_sanitized_successor(self):
        original, chm, _ = self.fixture.composed_history()
        state = json.loads(self.store.read_bytes(audit.ASSETS, publish.RETIREMENT))
        original_ref = publication.metadata(audit.ASSETS,
            f"reader-index/v3/generations/{original['generation']}/catalog.json", publication.encode(original))
        state['resources'] = [original_ref, state['rollback_replacements'][original['generation']]]
        self.state(**state)
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        items = {item['old_generation']: item for item in plan['replacement_plan']['replacements']}
        successor = items[chm['generation']]
        discarded = items[original['generation']]
        self.assertFalse(discarded['candidate_required'])
        self.apply(plan)
        after = json.loads(plan['after'])
        ref = after['rollback_replacements'][original['generation']]
        self.assertEqual(ref, successor['catalog_ref'])
        self.assertEqual(successor['catalog']['rollback_target'], original['generation'])
        self.assertNotIn(discarded['catalog_ref'], after['resources'])
        self.assertNotIn((audit.ASSETS, discarded['catalog_ref']['path']), self.store.objects)
        base = audit.decode_sidecar(successor['packed'])
        self.assertNotIn('VoiceOfML/Test\0unsafe.chm', base['f'])
        self.assertIn('safe.epub', base['f'])
        self.assertEqual(base['f'][KEY]['p'], NEW)
        self.assert_originals(before)

    def test_clean_existing_alias_wins_over_unsanitized_original_resource(self):
        clean, _ = redirects.rewrite(self.fixture.base, {OLD: NEW})
        _, sanitized = self.fixture.history(clean, rollback_target=self.old['generation'])
        ref = sanitized['resources'][0]
        old_ref = self.old_promotion['resources'][0]
        self.state(resources=[old_ref], retired_paths=[old_ref['path']],
                   rollback_replacements={self.old['generation']: ref})
        plan = self.prepare()
        self.apply(plan)
        after = json.loads(plan['after'])
        self.assertEqual(after['rollback_replacements'][self.old['generation']], ref)
        self.assertIn(ref, after['resources'])
        self.assertNotIn(old_ref, after['resources'])

    def test_unrelated_existing_alias_and_rollback_target_are_preserved(self):
        clean, _ = redirects.rewrite(self.fixture.base, {OLD: NEW})
        target = 'a' * 64
        catalog, promotion = self.fixture.history(clean, rollback_target=target)
        ref = promotion['resources'][0]
        self.state(resources=[ref], rollback_replacements={target: ref})
        plan = self.prepare()
        self.apply(plan)
        after = json.loads(plan['after'])
        self.assertEqual(after['rollback_replacements'][target], ref)
        self.assertIn(ref, after['resources'])
        self.assertNotIn(ref['path'], after['retired_paths'])
        self.assertEqual(publication.read_index(self.store, ref)['rollback_target'], target)

    def test_alias_clash_blocks_without_staging(self):
        self.fixture.composed_history(retired=False)
        before = copy.deepcopy(self.store.objects)
        with self.assertRaisesRegex(ValueError, 'alias conflict'):
            self.prepare()
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.writes, [])

    def test_actual_current_acks_and_receipts_required(self):
        raw = self.store.objects[audit.ASSETS, self.current_promotion]
        for mode in ('unacked', 'missing-receipts', 'wrong-generation', 'unverified', 'missing-date'):
            with self.subTest(mode=mode):
                promotion = json.loads(raw)
                if mode == 'unacked':
                    promotion['acks']['pages'] = False
                elif mode == 'missing-receipts':
                    del promotion['receipts']
                elif mode == 'wrong-generation':
                    promotion['receipts']['hf']['generation'] = self.old['generation']
                elif mode == 'unverified':
                    promotion['receipts']['hf']['projection_verified'] = False
                else:
                    del promotion['receipts']['hf']['checked_at']
                self.store.objects[audit.ASSETS, self.current_promotion] = publication.encode(promotion)
                with self.assertRaises(ValueError):
                    self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_explicit_generation_and_fresh_consumers_required_at_apply(self):
        plan = self.prepare()
        with self.assertRaisesRegex(ValueError, 'verified generation'):
            publish.apply(self.store, plan, current_ack_verified_generation=self.old['generation'],
                          observe_consumers=self.observe)
        self.receipts['pages']['generation'] = self.old['generation']
        with self.assertRaisesRegex(ValueError, 'current reading generation'):
            self.apply(plan)
        self.assertEqual(self.store.cas_attempts, 0)
        self.assertNotIn((audit.ASSETS, publish.RETIREMENT), self.store.objects)

    def test_current_selected_redirects_and_pinned_old_paths_block(self):
        for mode in ('redirect', 'base'):
            with self.subTest(mode=mode):
                catalog = copy.deepcopy(self.current)
                if mode == 'redirect':
                    catalog[redirects.FIELD] = {OLD: NEW}
                else:
                    catalog['base_sidecar'] = self.old['base_sidecar']
                del catalog['generation']
                catalog['generation'] = publication.text.digest(catalog)
                raw = publication.encode(catalog)
                ref = publication.metadata(audit.ASSETS,
                    f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw)
                self.store.objects[audit.ASSETS, ref['path']] = raw
                self.store.objects[audit.ASSETS, publication.POINTER] = publication.encode({
                    **self.pointer, 'generation': catalog['generation'], 'catalog': ref, 'resources': [ref]})
                with self.assertRaisesRegex(ValueError, 'finalization|selected old derived bundle'):
                    publish.prepare(self.store, {OLD: NEW}, current_ack_verified_generation=catalog['generation'],
                                    observe_consumers=self.observe)
        self.assertEqual(self.store.writes, [])

    def test_selected_legacy_refs_block_even_with_complete_inventory(self):
        legacy = {'version': 1, 'kind': 'reader-catalog', 'current': None, 'generations': {
            'legacy': {'status': 'old', 'references': [{'bucket': audit.BUCKET, 'path': OLD}]}}}
        self.store.objects[audit.ASSETS, migration.CATALOG] = publication.encode(legacy)
        report = inventory.inventory(self.store, {OLD: NEW})
        self.assertTrue(report['scope_complete'])
        with self.assertRaisesRegex(ValueError, 'selected legacy references'):
            self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_incomplete_inventory_or_plan_blockers_reject(self):
        original = replacement.prepare
        for field in ('blockers', 'scope_complete', 'discovery_mode', 'plan_blockers'):
            def blocked(*args, **kwargs):
                plan = original(*args, **kwargs)
                if field == 'blockers':
                    plan['history_inventory']['blockers'] = [{'code': 'test'}]
                elif field == 'scope_complete':
                    plan['history_inventory']['scope_complete'] = False
                elif field == 'plan_blockers':
                    plan['blockers'] = ['test']
                else:
                    plan[field] = 'explicit'
                return plan
            with self.subTest(field=field), patch.object(replacement, 'prepare', side_effect=blocked):
                with self.assertRaisesRegex(ValueError, 'scope complete'):
                    self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_changed_roots_evidence_new_catalog_and_copy_block_reviewed_plan(self):
        for mode in ('index', 'sidecar', 'legacy', 'pointer', 'promotion', 'base',
                     'retirement-created', 'new-history', 'copy'):
            with self.subTest(mode=mode):
                self.setUp()
                plan = self.prepare()
                paths = {'index': (audit.BUCKET, audit.INDEX), 'sidecar': (audit.ASSETS, audit.SIDECAR),
                         'legacy': (audit.ASSETS, migration.CATALOG),
                         'pointer': (audit.ASSETS, publication.POINTER),
                         'promotion': (audit.ASSETS, replacement.PROMOTIONS + self.old['generation'] + '.json'),
                         'base': (audit.ASSETS, self.old['base_sidecar']['path'])}
                if mode in paths:
                    self.store.objects[paths[mode]] += b' '
                elif mode == 'retirement-created':
                    self.state()
                elif mode == 'new-history':
                    self.fixture.history(metadata={'new-history': True})
                else:
                    self.store.api.files[NEW]['xet_hash'] = 'changed'
                before = copy.deepcopy(self.store.objects)
                with self.assertRaises((ValueError, OSError)):
                    self.apply(plan)
                self.assertEqual(self.store.objects, before)
                self.assertEqual(self.store.cas_attempts, 0)

    def test_staging_failure_never_mutates_existing_journal(self):
        self.state(resources=[self.old_promotion['resources'][0]])
        plan = self.prepare()
        before = copy.deepcopy(self.store.objects)
        def fail(path):
            if path.endswith('/catalog.json'):
                raise OSError('stage failed')
        self.store.stage_hook = fail
        with self.assertRaisesRegex(OSError, 'stage failed'):
            self.apply(plan)
        self.assertEqual(self.store.objects[audit.ASSETS, publish.RETIREMENT], plan['before'])
        self.assertEqual(self.store.cas_attempts, 0)
        self.assert_originals(before)

    def test_mutation_during_staging_or_intent_blocks_cas(self):
        for mode in ('pointer', 'evidence', 'retirement', 'intent', 'token'):
            with self.subTest(mode=mode):
                self.setUp()
                self.state()
                plan = self.prepare()
                def change(path):
                    if mode == 'intent' and not path.startswith(publish.INTENTS):
                        return
                    self.store.stage_hook = None
                    if mode in ('pointer', 'intent'):
                        self.store.objects[audit.ASSETS, publication.POINTER] += b' '
                    elif mode == 'evidence':
                        self.store.objects[audit.ASSETS, self.old['base_sidecar']['path']] += b' '
                    elif mode == 'token':
                        self.store.revision += 1
                    else:
                        self.state(unknown='concurrent')
                self.store.stage_hook = change
                with self.assertRaises((ValueError, OSError)):
                    self.apply(plan)
                self.assertEqual(self.store.cas_attempts, 0)
                self.assertEqual(sum(path == publish.RETIREMENT for _, path in self.store.writes), 0)

    def test_cas_readback_conflict_lost_response_and_unknown_never_retry(self):
        for mode, outcome in (('conflict', 'conflict'), ('lost-response', 'applied'),
                              ('unknown', 'unknown'), ('unreadable', 'unknown')):
            with self.subTest(mode=mode):
                self.setUp()
                self.state(unknown='preserved')
                plan = self.prepare()
                self.store.mode = mode
                if mode == 'unreadable':
                    # Allow the server to commit, then fail the client's readback.
                    original = self.store.put_if_match
                    def commit(*args):
                        self.store.mode = 'success'
                        original(*args)
                        self.store.mode = 'unreadable'
                    self.store.put_if_match = commit
                result = self.apply(plan)
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['applied'], outcome == 'applied')
                self.assertEqual(self.store.cas_attempts, 1)
                self.assertIn((audit.ASSETS, result['intent_ref']['path']), self.store.objects)
                if mode == 'conflict':
                    self.assertEqual(self.store.objects[audit.ASSETS, publish.RETIREMENT], b'{"foreign":true}')
                elif mode == 'unknown':
                    self.assertEqual(self.store.objects[audit.ASSETS, publish.RETIREMENT], plan['before'])

    def test_consumer_callback_root_drift_blocks_prepare_and_apply(self):
        for phase in ('prepare', 'apply'):
            with self.subTest(phase=phase):
                self.setUp()
                plan = self.prepare() if phase == 'apply' else None
                def drift(store, generation):
                    receipts = self.observe(store, generation)
                    store.objects[audit.ASSETS, publication.POINTER] += b' '
                    return receipts
                with self.assertRaisesRegex(ValueError, 'evidence changed|fresh evidence'):
                    if phase == 'prepare':
                        publish.prepare(self.store, {OLD: NEW},
                            current_ack_verified_generation=self.generation, observe_consumers=drift)
                    else:
                        publish.apply(self.store, plan,
                            current_ack_verified_generation=self.generation, observe_consumers=drift)
                self.assertEqual(self.store.cas_attempts, 0)
                self.assertNotIn((audit.ASSETS, publish.RETIREMENT), self.store.objects)

    def test_intent_stage_failure_leaves_before_bytes_and_no_cas(self):
        self.state(unknown='retain me')
        plan = self.prepare()
        def fail(path):
            if path.startswith(publish.INTENTS):
                raise OSError('intent stage response lost')
        self.store.stage_hook = fail
        with self.assertRaisesRegex(OSError, 'intent stage'):
            self.apply(plan)
        self.assertEqual(self.store.cas_attempts, 0)
        self.assertEqual(self.store.objects[audit.ASSETS, publish.RETIREMENT], plan['before'])

    def test_immutable_candidate_conflict_blocks_before_journal_write(self):
        plan = self.prepare()
        ref = plan['replacement_plan']['replacements'][0]['catalog_ref']
        self.store.objects[ref['bucket'], ref['path']] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'immutable history replacement conflict'):
            self.apply(plan)
        self.assertEqual(self.store.cas_attempts, 0)
        self.assertEqual(self.store.writes, [])

    def test_absent_root_creation_race_never_overwrites_foreign_journal(self):
        plan = self.prepare()
        self.store.mode = 'conflict'
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'conflict')
        self.assertEqual(self.store.cas_attempts, 1)
        self.assertEqual(self.store.objects[audit.ASSETS, publish.RETIREMENT], b'{"foreign":true}')

    def test_candidate_tampering_and_writer_gate_reject_before_staging(self):
        plan = self.prepare()
        self.store.writer = False
        with self.assertRaisesRegex(ValueError, 'writer window'):
            self.apply(plan)
        self.store.writer = True
        plan['replacement_plan']['replacements'][0]['catalog_raw'] += b' '
        with self.assertRaisesRegex(ValueError, 'fresh evidence'):
            self.apply(plan)
        self.assertEqual(self.store.writes, [])

    def test_only_standard_old_catalogs_can_be_retired(self):
        plan = replacement.prepare(self.store, {OLD: NEW})
        item = plan['replacements'][0]
        # Inventory supports derived histories, but this bounded publisher does
        # not retire that namespace in a subsequent cleanup operation.
        original = replacement.prepare
        def nonstandard(*args, **kwargs):
            value = original(*args, **kwargs)
            value['replacements'][0]['old_catalog']['path'] = item['catalog_ref']['path']
            return value
        with patch.object(replacement, 'prepare', side_effect=nonstandard):
            with self.assertRaisesRegex(ValueError, 'standard old catalog'):
                self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_immutable_allowlist_rejects_canonical_and_misaddressed_writes(self):
        for path in (audit.SIDECAR, publication.POINTER, publish.RETIREMENT,
                     publish.INTENTS + 'a' * 64 + '.json'):
            raw = b'{}'
            with self.subTest(path=path), self.assertRaises(ValueError):
                publish._immutable(self.store, publication.metadata(audit.ASSETS, path, raw), raw)
        self.assertEqual(self.store.writes, [])


if __name__ == '__main__':
    unittest.main()
