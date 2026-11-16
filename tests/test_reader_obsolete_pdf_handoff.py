"""Offline proof, CAS, and isolated maintenance tests for the exact handoff."""
import copy
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import reader_obsolete_pdf_handoff as handoff
from tests import test_reader_obsolete_pdf_apply as fixtures
from tests.test_reader_route_control import Consumers


class HandoffTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ObsoletePDFApplyTests()
        fixture.setUp()
        self.fixture, self.store, self.plan = fixture.fixture, fixture.store, fixture.plan
        retired = fixture.apply()
        self.store.events.clear()
        self.config = {'reference_intent': retired['intent_ref'],
                       'replacement_proofs': copy.deepcopy(self.plan['replacement_proofs']),
                       'processing_records': []}
        approved = {}
        paths = list(handoff.APPROVED)
        for number, path in enumerate(paths):
            caj = number == 0
            index = 7 if caj else number - 1
            root = self.fixture.copies[index]['old_pdf' if caj else 'new_pdf'].rsplit('/', 1)[0]
            record = handoff.lifecycle.processing_record([{'bucket': handoff.BUCKET, 'root': root}],
                'ccrd-body-evidence-upload' if caj else 'pdf-derived-object-copy',
                {'repository': 'original', 'run_id': 'original-run', 'attempt': '1'})
            if caj:
                record.update(status='uploaded', withdrawal={'kind': 'preserved', 'count': 21})
            row = {'path': path, 'document': record, 'matching_resources': record['resources']}
            self.config['processing_records'].append(row)
            approved[path] = (root, handoff.proof(handoff.publication.encode(record))['sha256'])
            # Unusual formatting is retained verbatim in intent.before.
            self.store.objects[handoff.ASSETS, path] = json.dumps(record, indent=3).encode() + b'\n'
            self.store.versions[handoff.ASSETS, path] = 17
        # Synthetic source bytes exercise real preparation, reference inspection,
        # publication consumers and graph generation resolution. Production pins
        # remain exact; tests substitute only their fixture identities.
        self.enterContext(patch.object(handoff, 'APPROVED', approved))
        self.enterContext(patch.object(handoff, 'REFERENCE_INTENT', retired['intent_ref']))
        self.enterContext(patch.object(handoff, 'REPLACEMENTS_SHA256',
            handoff.proof(handoff.publication.encode(self.config['replacement_proofs']))['sha256']))
        self.client = Consumers(self.fixture.store)
        self.metadata = lambda bucket, path: copy.deepcopy(self.fixture.meta[path])
        self.window = Mock(return_value=True)
        self.saved = []
        self.owner = {'repository': 'anftm/pipeline', 'run_id': '123', 'attempt': '1'}

    def observe(self):
        handoff.validate_config(self.config)
        return handoff.observe(self.store, self.client, self.config, self.metadata)

    def prepared(self):
        evidence = self.observe()
        return handoff.prepare(self.store, self.config, evidence, now='2026-11-15T12:00:00+00:00'), evidence

    def apply(self):
        mutations, evidence = self.prepared()
        def checkpoint(result):
            self.saved.append((copy.deepcopy(result), len(self.store.events)))
        result = handoff.apply(self.store, mutations, evidence, self.window, checkpoint, self.owner)
        return result, mutations, evidence

    def test_exact_handoff_preserves_ownership_and_uses_resolved_base(self):
        before = copy.deepcopy(self.store.objects)
        result, mutations, evidence = self.apply()
        self.assertEqual(result['outcome'], 'applied')
        self.assertFalse(result['deletion_authorized'])
        base = json.loads(before[handoff.graph.CATALOG_REF])
        self.assertEqual(evidence['generation'], base['current']['generation'])
        self.assertNotEqual(evidence['generation'], evidence['pointer']['generation'])
        ref = result['intent_ref']
        intent = json.loads(self.store.read_bytes(ref['bucket'], ref['path']))
        self.assertEqual(intent['owner'], self.owner)
        self.assertEqual(intent['evidence'], evidence)
        self.assertEqual(handoff.HandoffStore._immutable_type(ref['bucket'], ref['path'],
                         self.store.read_bytes(ref['bucket'], ref['path'])), 'application/json')
        self.assertEqual(self.store.events, [('intent', ref['bucket'], ref['path'])]
                         + [('cas', m['bucket'], m['path']) for m in mutations])
        self.assertEqual(self.saved[0][1], 0)
        pending = [(r, n) for r, n in self.saved if any(x['cas_attempts'] == 1
                   and x['outcome'] == 'unknown' for x in r['roots'])]
        self.assertEqual([n for _, n in pending], list(range(1, 9)))
        reference = handoff.graph.ReferenceGraph(self.store)
        reference.merge_catalog(base)
        self.assertTrue(reference.handoff_resolved(evidence['generation']))
        for m, packed in zip(mutations, intent['mutations']):
            self.assertEqual(handoff.operation._unpacked(packed), m)
            old, released = json.loads(m['before']), json.loads(m['after'])
            expected = handoff.lifecycle.release_processing(old, evidence['generation'],
                                                            '2026-11-15T12:00:00+00:00')
            self.assertEqual({k: v for k, v in released.items() if k != 'observations'}, expected)
            self.assertEqual(released['observations']['obsolete_pdf_handoff']['kind'],
                             'superseded-verified-replacement')
            reference.walk(released, m['bucket'], m['path'])
            self.assertIn((m['bucket'], m['path']), reference.released_processing)
        mutable = {(m['bucket'], m['path']) for m in mutations}
        self.assertEqual({k: v for k, v in before.items() if k not in mutable},
                         {k: v for k, v in self.store.objects.items() if k in before and k not in mutable})
        self.assertEqual(set(self.store.objects) - set(before), {(ref['bucket'], ref['path'])})
        inspection = handoff.inspect(self.store, ref)
        self.assertEqual([r['outcome'] for r in inspection['roots']], ['after'] * 8)
        self.assertFalse(inspection['resume_authorized'])
        self.assertFalse(handoff.graph.is_root(ref['path']))
        with self.assertRaisesRegex(ValueError, 'record changed'):
            handoff.prepare(self.store, self.config, evidence)

    def test_config_rejects_wrong_roots_records_paths_and_proofs(self):
        original = copy.deepcopy(self.config)
        for mode in ('root', 'run', 'status', 'path', 'duplicate', 'missing', 'intent', 'proof', 'wrapper'):
            with self.subTest(mode=mode):
                self.config = copy.deepcopy(original)
                row = self.config['processing_records'][0]
                if mode == 'root':
                    row['document']['resources'][0]['root'] += '/foreign'
                elif mode in ('run', 'status'):
                    row['document'][mode] = 'foreign'
                elif mode == 'path':
                    row['path'] = 'reader-index/processing/foreign.json'
                elif mode == 'duplicate':
                    self.config['processing_records'][1] = row
                elif mode == 'missing':
                    self.config['processing_records'].pop()
                elif mode == 'intent':
                    self.config['reference_intent']['sha256'] = '0' * 64
                elif mode == 'wrapper':
                    self.config['replacement_proofs'] = {'base64': 'not-a-proof'}
                else:
                    self.config['replacement_proofs'][0]['pdf']['sha256'] = '0' * 64
                with self.assertRaises(ValueError):
                    handoff.validate_config(self.config)
                self.assertEqual(self.store.events, [])

    def test_changed_live_record_rejected_before_intent(self):
        evidence = self.observe()
        row = self.config['processing_records'][0]
        changed = {**row['document'], 'heartbeat_at': 'later'}
        self.store.objects[handoff.ASSETS, row['path']] = handoff.publication.encode(changed)
        with self.assertRaisesRegex(ValueError, 'record changed'):
            handoff.prepare(self.store, self.config, evidence)
        self.assertEqual(self.store.events, [])

    def test_original_roots_must_all_be_after(self):
        for mutation in self.plan['mutations']:
            key = mutation['bucket'], mutation['path']
            self.store.objects[key] = mutation['before']
            with self.assertRaisesRegex(ValueError, 'not all after'):
                self.observe()
            self.store.objects[key] = mutation['after']
        self.assertEqual(self.store.events, [])

    def test_unverified_or_stale_consumers_rejected(self):
        real = handoff.publication.observe_consumer
        for field, value in [('projection_verified', False), ('active', False),
                             ('generation', 'stale'), ('catalog_sha256', 'foreign'), ('files', -1)]:
            with self.subTest(field=field):
                def stale(store, site, client):
                    receipt = real(store, site, client)
                    return {**receipt, field: value} if site == 'pages' else receipt
                with patch.object(handoff.publication, 'observe_consumer', side_effect=stale):
                    with self.assertRaisesRegex(ValueError, 'unverified current consumer'):
                        self.observe()
        self.assertEqual(self.store.events, [])

    def test_changed_active_pdf_full_hash_and_metadata_rejected(self):
        for replacement in self.config['replacement_proofs']:
            path = replacement['pdf']['path']
            key = handoff.BUCKET, path
            original = self.store.objects[key]
            # Same size, different content: size and Xet metadata alone cannot pass.
            self.store.objects[key] = b'!' + original[1:]
            with self.assertRaisesRegex(ValueError, 'active PDF content changed'):
                self.observe()
            self.store.objects[key] = original
            self.fixture.meta[path]['xet_hash'] = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'active PDF metadata changed'):
                self.observe()
            self.fixture.meta[path]['xet_hash'] = replacement['pdf']['metadata']['xet_hash']
        self.assertEqual(self.store.events, [])

    def test_mapping_must_cover_exact_thirty_targets_and_all_eight_resources(self):
        intent = handoff.reference_after(self.store, self.config)
        projection = self.fixture.projection
        bad = copy.deepcopy(intent)
        bad['target_delete_paths'][-1] = 'objects/foreign/document.pdf'
        with self.assertRaisesRegex(ValueError, 'pairs'):
            handoff.verify_replacements(self.store, self.config, bad, self.metadata, projection)
        approved = copy.deepcopy(handoff.APPROVED)
        path = next(iter(approved))
        approved[path] = ('derived/foreign/root', approved[path][1])
        with patch.object(handoff, 'APPROVED', approved):
            with self.assertRaisesRegex(ValueError, 'map exactly'):
                handoff.verify_replacements(self.store, self.config, intent, self.metadata, projection)
        old_path = self.fixture.copies[0]['old_manifest']
        self.store.objects[handoff.BUCKET, old_path] += b' '
        with self.assertRaisesRegex(ValueError, 'source manifest changed'):
            self.observe()

    def test_cas_unknown_stops_and_never_retries_or_rolls_back(self):
        mutations, evidence = self.prepared()
        objects = copy.deepcopy(self.store.objects)
        for mode, expected in [('before', 'unknown'), ('noop', 'unknown'),
                               ('unreadable', 'unknown'), ('foreign', 'conflict')]:
            with self.subTest(mode=mode):
                store = fixtures.ConditionalStore(copy.deepcopy(objects))
                store.behavior = lambda b, p: mode
                result = handoff.apply(store, mutations, evidence, self.window, Mock(), self.owner)
                self.assertEqual(result['outcome'], 'partial')
                self.assertEqual(result['roots'][0]['outcome'], expected)
                self.assertEqual([r['cas_attempts'] for r in result['roots']], [1] + [0] * 7)
                self.assertEqual([r['outcome'] for r in result['roots'][1:]], ['not_attempted'] * 7)
                self.assertEqual([e[0] for e in store.events], ['intent', 'cas'])

    def test_lost_response_after_exact_write_is_recognized(self):
        self.store.behavior = lambda b, p: 'lost'
        result, _, _ = self.apply()
        self.assertEqual(result['outcome'], 'applied')
        self.assertTrue(all(r['cas_attempts'] == 1 and r['cas_error'] == 'OSError' for r in result['roots']))
        self.assertEqual(len(self.store.events), 9)

    def test_checkpoint_failure_and_intent_failure_prevent_mutable_write(self):
        mutations, evidence = self.prepared()
        def fail(result):
            if result['intent_verified']:
                raise OSError('durable journal unavailable')
        with self.assertRaises(OSError):
            handoff.apply(self.store, mutations, evidence, self.window, fail, self.owner)
        self.assertEqual([e[0] for e in self.store.events], ['intent'])
        self.store.events.clear()
        self.store.on_stage = lambda b, p: self.store.objects.__setitem__((b, p), b'bad')
        result = handoff.apply(self.store, mutations, evidence, self.window, Mock(), self.owner)
        self.assertFalse(result['intent_verified'])
        self.assertEqual([e[0] for e in self.store.events], ['intent'])

    def test_changed_token_or_window_loss_stops_without_cas(self):
        mutations, evidence = self.prepared()
        key = mutations[0]['bucket'], mutations[0]['path']
        self.store.on_stage = lambda b, p: self.store.versions.__setitem__(key, 99)
        result = handoff.apply(self.store, mutations, evidence, self.window, Mock(), self.owner)
        self.assertEqual(result['roots'][0]['outcome'], 'conflict')
        self.assertEqual(len(self.store.events), 1)
        self.store.events.clear()
        self.store.on_stage = None
        self.window.side_effect = [True, True, False]
        result = handoff.apply(self.store, mutations, evidence, self.window, Mock(), self.owner)
        self.assertEqual(result['roots'][0]['outcome'], 'unknown')
        self.assertEqual(len(self.store.events), 1)

    def test_post_checkpoint_window_and_intent_checks_precede_every_write(self):
        mutations, evidence = self.prepared()
        original = copy.deepcopy(self.store.objects)
        for boundary in ('intent', 'first-cas', 'later-cas'):
            for drift in ('window', 'intent', 'record'):
                if boundary == 'intent' and drift != 'window':
                    continue
                with self.subTest(boundary=boundary, drift=drift):
                    store = fixtures.ConditionalStore(copy.deepcopy(original))
                    window = Mock(return_value=True)
                    expected_cas = 1 if boundary == 'later-cas' else 0
                    changed = []
                    def checkpoint(result):
                        pending = [r for r in result['roots'] if r['cas_attempts'] == 1
                                   and r['outcome'] == 'unknown']
                        reached = (not result['intent_verified'] if boundary == 'intent' else
                                   bool(pending) and len(store.events) == expected_cas + 1)
                        if not reached or changed:
                            return
                        changed.append(True)
                        if drift == 'window':
                            window.return_value = False
                        elif drift == 'intent':
                            ref = result['intent_ref']
                            store.objects[ref['bucket'], ref['path']] += b' '
                        else:
                            row = pending[0]
                            store.objects[row['bucket'], row['path']] += b' '
                    result = handoff.apply(store, mutations, evidence, window, checkpoint, self.owner)
                    self.assertTrue(changed)
                    self.assertEqual(result['outcome'], 'partial')
                    self.assertEqual(sum(e[0] == 'cas' for e in store.events), expected_cas)
                    self.assertEqual(sum(r['cas_attempts'] for r in result['roots']), expected_cas)
                    if boundary == 'intent':
                        self.assertEqual(store.events, [])
                    else:
                        self.assertEqual(result['roots'][expected_cas]['outcome'], 'unknown')

    def test_canonical_drift_hidden_by_v3_override_is_rejected_before_projection(self):
        pointer, catalog = handoff.publication.current(self.store)
        pinned = handoff.publication.pinned_base(self.store, catalog, None)
        key = next(iter(pinned['f']))
        canonical = copy.deepcopy(pinned)
        canonical['f'][key]['p'] = 'objects/foreign/document.pdf'
        # Simulate an override masking that exact base route. The old check
        # compared only these equal projections and missed the base drift.
        project = handoff.publication.project_sidecar
        def masking(base, current):
            result = project(base, current)
            result['f'][key] = pinned['f'][key]
            return result
        self.store.objects[handoff.ASSETS, handoff.p.audit.SIDECAR] = gzip.compress(
            handoff.publication.encode(canonical))
        with patch.object(handoff.publication, 'project_sidecar', side_effect=masking) as projection:
            self.assertEqual(masking(canonical, catalog), masking(pinned, catalog))
            with self.assertRaisesRegex(ValueError, 'full pinned base'):
                self.observe()
            projection.assert_not_called()
        self.assertEqual(self.store.events, [])

    def test_adapter_has_exact_roots_after_bytes_namespace_and_single_attempt(self):
        mutations, evidence = self.prepared()
        s3, client = Mock(), Mock()
        s3._location.side_effect = lambda bucket: tuple(bucket.split('/'))
        s3._client.return_value = client
        adapter = handoff.HandoffStore(Mock(), mutations, s3)
        row = mutations[0]
        for bucket, path, raw, token in [(handoff.BUCKET, row['path'], row['after'], '"a"'),
                (handoff.ASSETS, 'reader-index/reader_catalog.json', row['after'], '"a"'),
                (handoff.ASSETS, row['path'], b'bad', '"a"'),
                (handoff.ASSETS, row['path'], row['after'], None)]:
            with self.assertRaises(ValueError):
                adapter.put_if_match(bucket, path, raw, token)
        client.put_object.assert_not_called()
        client.put_object.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            adapter.put_if_match(row['bucket'], row['path'], row['after'], '"a"')
        client.put_object.assert_called_once()
        self.assertEqual(client.put_object.call_args.kwargs['IfMatch'], '"a"')
        result, _, _ = self.apply()
        ref = result['intent_ref']
        raw = self.store.read_bytes(ref['bucket'], ref['path'])
        for path, body in [(handoff.operation.INTENTS + ref['sha256'] + '.json', raw),
                           (ref['path'], raw + b' ')]:
            with self.assertRaises(ValueError):
                adapter._immutable_type(handoff.ASSETS, path, body)

    def test_main_durable_owner_checkpoint_and_restore_safe_callback(self):
        self.check_main(partial=True)

    def test_main_accepts_collective_readback_and_rechecks_live_evidence(self):
        self.check_main(partial=False)

    def test_main_rejects_observation_and_acceptance_checkpoint_drift(self):
        for phase in ('observation', 'checkpoint'):
            for target in ('root', 'intent', 'window'):
                with self.subTest(phase=phase, target=target):
                    self.setUp()
                    self.check_main(partial=False, drift=(phase, target))

    def check_main(self, *, partial, drift=None):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config, journal, output = [directory / (n + '.json') for n in ('config', 'journal', 'output')]
            config.write_bytes(handoff.publication.encode(self.config))
            state, restored, checkpoints = {}, [], []
            def maintenance_run(path, *, isolated_callback, suppress_resume_dispatch, preserve_queued_runs):
                self.assertTrue(suppress_resume_dispatch)
                self.assertTrue(preserve_queued_runs)
                try:
                    return isolated_callback(state)
                finally:
                    restored.append(True)
            def corrupt():
                target = drift[1]
                if target == 'root':
                    key = handoff.ASSETS, self.config['processing_records'][0]['path']
                elif target == 'intent':
                    ref = state['obsolete_pdf_handoff']['operation']['intent_ref']
                    key = ref['bucket'], ref['path']
                else:
                    key = handoff.graph.CATALOG_REF
                self.store.objects[key] += b' '
            def checkpoint(path, value):
                path.write_bytes(handoff.publication.encode(value))
                checkpoints.append(copy.deepcopy(value))
                if drift and drift[0] == 'checkpoint' and 'acceptance' in value['obsolete_pdf_handoff']:
                    corrupt()
            real_observe = handoff.observe
            observations = []
            def observe(*args):
                value = real_observe(*args)
                observations.append(value)
                if drift and drift[0] == 'observation' and len(observations) == 2:
                    corrupt()
                return value
            hub = Mock()
            hub.api.get_bucket_paths_info.side_effect = lambda bucket, paths: [
                SimpleNamespace(size=self.fixture.meta[path]['size'], xet_hash=self.fixture.meta[path]['xet_hash'])
                for path in paths]
            env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
                   'GITHUB_WORKFLOW_REF': 'anftm/pipeline/.github/workflows/reader-maintenance.yml@refs/heads/main',
                   'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '123'}
            with patch.dict('os.environ', env, clear=True), \
                    patch('sys.argv', ['handoff', '--config', str(config), '--journal', str(journal), '--output', str(output)]), \
                    patch.object(handoff.maintenance, 'run', side_effect=maintenance_run), \
                    patch.object(handoff.maintenance, 'journal', side_effect=checkpoint), \
                    patch.object(handoff, 'CollectionStore', return_value=self.store), \
                    patch.object(handoff, 'HubBucketStore', return_value=hub), \
                    patch.object(handoff, 'HandoffStore', return_value=self.store), \
                    patch.object(handoff, 'GitHubProducerFence'), \
                    patch.object(handoff, 'observe', side_effect=observe), \
                    patch.object(handoff.reader_gc_chm_window, 'require_clear'), \
                    patch.object(handoff.httpx, 'Client') as client:
                client.return_value.__enter__.return_value = self.client
                if partial:
                    self.store.behavior = lambda b, p: 'before'
                    with self.assertRaisesRegex(ValueError, 'partial handoff'):
                        handoff.main()
                elif drift:
                    with self.assertRaises(ValueError):
                        handoff.main()
                else:
                    handoff.main()
                self.assertEqual(restored, [True])
                result = json.loads(output.read_bytes())
                self.assertEqual(result['operation']['owner'], self.owner)
                self.assertEqual(state['obsolete_pdf_handoff'], result)
                self.assertEqual(checkpoints[0]['obsolete_pdf_handoff']['operation']['roots'][0]['cas_attempts'], 0)
                self.assertEqual(checkpoints[1]['obsolete_pdf_handoff']['operation']['roots'][0]['cas_attempts'], 1)
                if partial or drift:
                    self.assertNotIn('acceptance', result)
                    self.assertEqual(len(self.store.events), 2 if partial else 9)
                else:
                    self.assertEqual(result['operation']['outcome'], 'applied')
                    self.assertEqual([r['outcome'] for r in result['acceptance']['roots']['roots']], ['after'] * 8)
                    self.assertEqual(len(self.store.events), 9)

    def test_current_coverage_keeps_references_shared_with_earlier_live_generation(self):
        base = json.loads(self.store.objects[handoff.graph.CATALOG_REF])
        generation = base['current']['generation']
        current = base['generations'][generation]
        earlier_id = 'earlier-live-generation'
        earlier = {**copy.deepcopy(current), 'generation': earlier_id, 'status': 'superseded'}
        # No replacement acknowledgments: this earlier generation remains live.
        base['generations'] = {earlier_id: earlier, generation: current}
        raw = json.dumps(base).encode()  # Preserve earlier-before-current order.
        self.store.objects[handoff.graph.CATALOG_REF] = raw
        lossy = handoff.graph.ReferenceGraph(self.store).load(handoff.graph.CATALOG_REF)
        self.assertEqual(lossy['generations'][generation]['references'], [])
        self.assertEqual(self.observe()['generation'], generation)

        def streamed(bucket, path, parser=None):
            self.assertEqual((bucket, path), handoff.graph.CATALOG_REF)
            self.assertIsNotNone(parser)
            return parser((raw[n:n + 19] for n in range(0, len(raw), 19)), False)
        read_bytes = self.store.read_bytes
        def no_whole_catalog(bucket, path):
            self.assertNotEqual((bucket, path), handoff.graph.CATALOG_REF)
            return read_bytes(bucket, path)
        with patch.object(self.store, 'read_graph_json', side_effect=streamed, create=True) as stream, \
                patch.object(self.store, 'read_bytes', side_effect=no_whole_catalog):
            evidence = self.observe()
        stream.assert_called_once()
        self.assertEqual(evidence['generation'], generation)
        self.assertEqual(evidence['base_catalog_sha256'], handoff.proof(raw)['sha256'])
        self.assertEqual(self.store.events, [])

    def test_base_catalog_must_resolve_and_bind_current_sidecar(self):
        original = self.store.objects[handoff.graph.CATALOG_REF]
        for mode in ('missing', 'unbound', 'pending', 'generation', 'status', 'references', 'malformed'):
            base = json.loads(original)
            generation = base['current']['generation']
            if mode == 'missing':
                del base['generations'][generation]
            elif mode == 'unbound':
                base['generations'][generation]['sidecar_sha256'] = '0' * 64
            elif mode == 'pending':
                base['redirect_sidecar_pending'] = True
            elif mode == 'generation':
                base['generations'][generation]['generation'] = 'foreign'
            elif mode == 'status':
                base['generations'][generation]['status'] = 'superseded'
            elif mode == 'references':
                base['generations'][generation]['references'].pop()
            else:
                base['generations'][generation]['references'] = [{'bucket': handoff.BUCKET, 'path': None}]
            self.store.objects[handoff.graph.CATALOG_REF] = handoff.publication.encode(base)
            with self.assertRaises(ValueError):
                self.observe()
        self.assertEqual(self.store.events, [])


if __name__ == '__main__':
    unittest.main()
