import ast
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import chm_single_handoff_shadow as shadow, chm_single_interrupted_preflight as fixed


class MemoryCAS:
    """Test double only: not an S3 implementation or an isolation fence."""
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.calls = []

    def write(self, key, expected_etag, value, outcome='success'):
        self.calls.append((key, expected_etag))
        if self.state['etags'][key] != expected_etag:
            raise ValueError('CAS conflict')
        if outcome == 'timeout-before':
            raise TimeoutError('unknown outcome')
        self.state[key] = copy.deepcopy(value)
        self.state['etags'][key] = f'"{key}-changed"'
        if outcome == 'timeout-after':
            raise TimeoutError('unknown outcome')


class ChmSingleHandoffShadowTests(unittest.TestCase):
    def setUp(self):
        protection = {
            'version': 1, 'kind': 'reader-processing-roots', 'status': 'processing',
            'operation': 'reader-stream-upload', 'release_policy': 'explicit-publisher-handoff',
            'run': {'repository': 'anftm/pipeline', 'run_id': str(fixed.SOURCE_RUN), 'attempt': '1'},
            'resources': [{'bucket': fixed.ASSETS, 'root': fixed.ROOT}],
            'note': '原始字节',
        }
        # Exercise the real validate_processing implementation, patching only
        # the pinned hash to match this synthetic record (not its behavior).
        self.raw = (json.dumps(protection, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        self.hash_patch = patch.object(fixed, 'PROCESSING_SHA256', hashlib.sha256(self.raw).hexdigest())
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)
        pins = {}
        for index in range(51):
            path = f'reader-index/processing/stream-upload-{index:032x}.json'
            root = f'chapters/ebook/epub/{index:064x}/0123456789abcdef'
            record = {'version': 1, 'kind': 'reader-processing-roots', 'status': 'processing',
                      'operation': 'reader-stream-upload', 'release_policy': 'explicit-publisher-handoff',
                      'run': {'repository': 'anftm/pipeline', 'run_id': '41172648658', 'attempt': '1'},
                      'resources': [{'bucket': fixed.ASSETS, 'root': root}]}
            owner = {'key': f'fixture/repo\0book-{index}', 'manifest': root + '/chapter-manifest.json',
                     'processing_roots': [path]}
            pins[path] = {'raw': json.dumps(record).encode(), 'etag': f'"pin-{index}"', 'owner': owner}
        self.before = {
            'sidecar': {'v': 1, 'f': {fixed.SOURCE_KEY: fixed.route(fixed.OLD_ROOT), 'other': {'s': 1}}},
            'catalog': {'version': 1, 'kind': 'reader-catalog',
                        'current': {'generation': 'old', 'published_at': 'then'},
                        'generations': {'old': {'generation': 'old', 'status': 'current',
                                                'references': [{'bucket': fixed.ASSETS, 'path': 'old'}]},
                                        'history': {'generation': 'history', 'status': 'superseded',
                                                    'references': [{'bucket': fixed.ASSETS, 'path': 'historic'}],
                                                    'replacement_acks': {'hf': True, 'pages': True}}},
                        'retired_generations': {'retired': {'generation': 'retired'}}},
            'v3_pointer': {'generation': fixed.PARENT_V3},
            'processing': self.raw,
            'legacy_pins': pins,
            'etags': {key: f'"{key}-original"' for key in shadow.ETAG_KEYS},
            'guards': {'chm_index_sha256': fixed.INDEX_SHA256, 'maintenance_sha256': fixed.MAINTENANCE_SHA256,
                       'retirement_hints_sha256': '3' * 64},
        }
        identity_rows = {path: {'etag': value['etag'], 'owner_key': value['owner']['key']}
                         for path, value in pins.items()}
        self.inventory_patch = patch.object(shadow, 'LEGACY_PIN_IDENTITY_SHA256',
                                            shadow.legacy_pin_identity(identity_rows))
        self.inventory_patch.start()
        self.addCleanup(self.inventory_patch.stop)
        self.after = copy.deepcopy(self.before['sidecar'])
        self.after['f'][fixed.SOURCE_KEY] = fixed.route(fixed.ROOT)

    def plan(self):
        return shadow.make_plan(self.before, self.after, '2026-11-03T00:00:00Z')

    def test_raw_bytes_are_validated_then_plan_is_json_canonicalizable(self):
        plan = self.plan()
        self.assertEqual(plan['before']['processing'], {'sha256': hashlib.sha256(self.raw).hexdigest(),
                                                         'bytes': len(self.raw)})
        plan = json.loads(json.dumps(plan, ensure_ascii=False))
        self.assertEqual(shadow.inspect(plan, self.before)['observation'], 'planned')
        self.assertEqual(self.before['processing'], self.raw)
        for value in (self.raw.decode('utf-8'), b'{}', self.raw + b' '):
            before = {**self.before, 'processing': value}
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                shadow.make_plan(before, self.after, '2026-11-03T00:00:00Z')

    def test_all_history_pins_and_parent_references_are_preserved(self):
        plan = self.plan()
        before, after = plan['before'], plan['after']
        catalog = after['catalog']
        self.assertEqual(set(catalog['generations']), {'old', 'history', plan['base_generation']})
        self.assertEqual(catalog['generations']['history'], before['catalog']['generations']['history'])
        old = copy.deepcopy(before['catalog']['generations']['old'])
        old.update(status='superseded', superseded_at=plan['timestamp'])
        self.assertEqual(catalog['generations']['old'], old)
        self.assertEqual(catalog['retired_generations'], before['catalog']['retired_generations'])
        self.assertIn({'bucket': fixed.ASSETS, 'path': 'old'},
                      catalog['generations'][plan['base_generation']]['references'])
        self.assertEqual(after['legacy_pins'], before['legacy_pins'])
        self.assertEqual(after['processing'], before['processing'])
        self.assertEqual(after['guards'], before['guards'])
        # Snapshot publication/serialization and v3 remain deliberately absent.
        self.assertNotIn('snapshot', catalog['generations'][plan['base_generation']])

    def test_observed_partial_states_never_authorize_resume_or_gc(self):
        plan, memory = self.plan(), MemoryCAS(self.before)
        for expected in ('planned', 'sidecar-observed', 'catalog-observed'):
            report = shadow.inspect(plan, memory.state)
            self.assertEqual(report['observation'], expected)
            for name in ('recovery_authorized', 'external_writers_fenced',
                         'consumer_acceptance_proved', 'gc_authorized'):
                self.assertIs(report[name], False)
            self.assertEqual(memory.state['processing'], self.raw)
            self.assertEqual(memory.state['legacy_pins'], self.before['legacy_pins'])
            self.assertEqual(memory.state['guards'], self.before['guards'])
            if expected == 'planned':
                memory.write('sidecar', self.before['etags']['sidecar'], plan['after']['sidecar'])
            elif expected == 'sidecar-observed':
                memory.write('catalog', self.before['etags']['catalog'], plan['after']['catalog'])
        with self.assertRaisesRegex(RuntimeError, 'does not authorize'):
            shadow.require_real_activation(plan)

    def test_cas_conflict_and_uncertain_write_never_trigger_a_blind_retry(self):
        plan = self.plan()
        for key in ('sidecar', 'catalog'):
            for outcome in ('timeout-before', 'timeout-after'):
                memory = MemoryCAS(self.before)
                if key == 'catalog':
                    memory.write('sidecar', self.before['etags']['sidecar'], plan['after']['sidecar'])
                calls_before = len(memory.calls)
                with self.subTest(key=key, outcome=outcome), self.assertRaises(TimeoutError):
                    memory.write(key, self.before['etags'][key], plan['after'][key], outcome)
                report = shadow.inspect(plan, memory.state)
                self.assertFalse(report['recovery_authorized'])
                self.assertEqual(len(memory.calls), calls_before + 1)
                self.assertEqual(memory.state['legacy_pins'], self.before['legacy_pins'])
        memory = MemoryCAS(self.before)
        memory.state['etags']['sidecar'] = '"foreign"'
        with self.assertRaisesRegex(ValueError, 'CAS conflict'):
            memory.write('sidecar', self.before['etags']['sidecar'], plan['after']['sidecar'])
        with self.assertRaisesRegex(ValueError, 'ETag'):
            shadow.inspect(plan, memory.state)
        self.assertEqual(len(memory.calls), 1)

    def test_single_key_cas_cannot_detect_other_object_race(self):
        plan, memory = self.plan(), MemoryCAS(self.before)
        # A foreign writer changes the index after a precheck. A CAS against
        # the sidecar still succeeds: this is why inspection cannot authorize
        # recovery without a durable external-writer fence and fresh checks.
        memory.state['guards']['chm_index_sha256'] = '4' * 64
        memory.write('sidecar', self.before['etags']['sidecar'], plan['after']['sidecar'])
        with self.assertRaisesRegex(ValueError, 'CHM index'):
            shadow.inspect(plan, memory.state)
        self.assertEqual(memory.state['processing'], self.raw)
        self.assertEqual(memory.state['legacy_pins'], self.before['legacy_pins'])

    def test_aba_is_not_an_authorization_even_when_bytes_and_etag_match(self):
        plan, memory = self.plan(), MemoryCAS(self.before)
        foreign = copy.deepcopy(memory.state['sidecar'])
        foreign['f']['other'] = {'s': 9}
        memory.write('sidecar', self.before['etags']['sidecar'], foreign)
        # Model an object store that repeats a content-derived ETag on revert.
        memory.state['sidecar'] = copy.deepcopy(self.before['sidecar'])
        memory.state['etags']['sidecar'] = self.before['etags']['sidecar']
        report = shadow.inspect(plan, memory.state)
        self.assertEqual(report['observation'], 'planned')
        self.assertFalse(report['external_writers_fenced'])
        self.assertFalse(report['recovery_authorized'])
        self.assertFalse(report['consumer_acceptance_proved'])

    def test_any_other_control_pin_or_hint_drift_fails_closed(self):
        plan = self.plan()
        for field in ('processing', 'legacy_pins', 'v3_pointer', 'catalog', 'sidecar', 'guards'):
            with self.subTest(field=field):
                current = copy.deepcopy(self.before)
                current[field] = {'foreign': True}
                with self.assertRaises(ValueError):
                    shadow.inspect(plan, current)
        for key in shadow.ETAG_KEYS:
            with self.subTest(etag=key):
                current = copy.deepcopy(self.before)
                current['etags'][key] = '"changed"'
                with self.assertRaises(ValueError):
                    shadow.inspect(plan, current)
        current = copy.deepcopy(self.before)
        current['catalog'] = copy.deepcopy(plan['after']['catalog'])
        current['etags']['catalog'] = '"catalog-changed"'
        with self.assertRaisesRegex(ValueError, 'mixed or foreign'):
            shadow.inspect(plan, current)

    def test_plan_checksum_and_rehashed_semantic_tampering_are_rejected(self):
        plan = self.plan()
        tampered = copy.deepcopy(plan)
        tampered['base_generation'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            shadow.inspect(tampered, self.before)
        for mutation in ('history', 'pins', 'route', 'v3', 'hints'):
            tampered = copy.deepcopy(plan)
            if mutation == 'history':
                del tampered['after']['catalog']['generations']['history']
            elif mutation == 'pins':
                tampered['after']['legacy_pins'].pop(next(iter(tampered['after']['legacy_pins'])))
            elif mutation == 'route':
                tampered['after']['sidecar']['f']['other'] = {'s': 2}
            elif mutation == 'v3':
                tampered['after']['v3_pointer'] = {'generation': 'foreign'}
            else:
                tampered['after']['guards']['retirement_hints_sha256'] = '4' * 64
            tampered['plan_sha256'] = shadow.digest({k: v for k, v in tampered.items() if k != 'plan_sha256'})
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'semantics'):
                shadow.inspect(tampered, self.before)

    def test_invalid_pin_inventory_etags_metadata_and_timestamps_fail(self):
        mutations = []
        value = copy.deepcopy(self.before)
        value['legacy_pins'].pop(next(iter(value['legacy_pins'])))
        mutations.append(value)
        value = copy.deepcopy(self.before)
        value['legacy_pins'][next(iter(value['legacy_pins']))]['raw'] = b'{"status":"released"}'
        mutations.append(value)
        value = copy.deepcopy(self.before)
        pin = value['legacy_pins'][next(iter(value['legacy_pins']))]
        record = json.loads(pin['raw'])
        record['status'] = 'released'
        pin['raw'] = json.dumps(record).encode()
        mutations.append(value)
        value = copy.deepcopy(self.before)
        pin = value['legacy_pins'][next(iter(value['legacy_pins']))]
        record = json.loads(pin['raw'])
        record['run']['run_id'] = '2'
        pin['raw'] = json.dumps(record).encode()
        mutations.append(value)
        value = copy.deepcopy(self.before)
        pin = value['legacy_pins'][next(iter(value['legacy_pins']))]
        pin['owner']['manifest'] = 'chapters/ebook/epub/foreign/chapter-manifest.json'
        mutations.append(value)
        value = copy.deepcopy(self.before)
        value['legacy_pins'][next(iter(value['legacy_pins']))]['etag'] = 'W/"weak"'
        mutations.append(value)
        value = copy.deepcopy(self.before)
        value['legacy_pins'][next(iter(value['legacy_pins']))]['owner']['key'] = 'foreign/repo\0book'
        mutations.append(value)
        value = copy.deepcopy(self.before)
        value['legacy_pins'][next(iter(value['legacy_pins']))]['etag'] = '"changed"'
        mutations.append(value)
        value = copy.deepcopy(self.before)
        value['etags']['catalog'] = 'W/"weak"'
        mutations.append(value)
        value = copy.deepcopy(self.before)
        value['guards'].pop('retirement_hints_sha256')
        mutations.append(value)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(ValueError):
                shadow.make_plan(value, self.after, '2026-11-03T00:00:00Z')
        for timestamp in ('now', '2026-11-03T00:00:00', ''):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                shadow.make_plan(self.before, self.after, timestamp)
        changed = {**self.after, 'extra': 'not-allowed'}
        with self.assertRaisesRegex(ValueError, 'metadata'):
            shadow.make_plan(self.before, changed, '2026-11-03T00:00:00Z')

    def test_gc_stub_or_elided_history_cannot_be_used_as_full_catalog(self):
        for mutation in ('missing_references', 'stub', 'bad_identity', 'invalid_reference'):
            before = copy.deepcopy(self.before)
            history = before['catalog']['generations']['history']
            if mutation == 'missing_references':
                del history['references']
            elif mutation == 'stub':
                history['gc_stub'] = True
            elif mutation == 'bad_identity':
                history['generation'] = 'foreign'
            else:
                history['references'] = [{'path': 'missing-bucket'}]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                shadow.make_plan(before, self.after, '2026-11-03T00:00:00Z')

    def test_model_has_no_storage_network_or_activation_entrypoint(self):
        source = Path(shadow.__file__).read_text()
        tree = ast.parse(source)
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(imports & {'os', 'sys', 'httpx', 'requests', 'boto3', 'subprocess', 'pathlib'})
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse(calls & {'put_object', 'put_bytes', 'delete', 'publish_catalog',
                                  'release_processing', 'processing_handoffs'})
        self.assertNotIn("if __name__ == '__main__'", source)
        self.assertNotIn('--apply', source)
        workflows = Path(shadow.__file__).resolve().parents[1] / '.github/workflows'
        self.assertFalse([path for path in workflows.glob('*.yml')
                          if 'scripts/chm_single_handoff_shadow.py' in path.read_text()])


if __name__ == '__main__':
    unittest.main()
