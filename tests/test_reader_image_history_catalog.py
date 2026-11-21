import copy
from decimal import Decimal
import gzip
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from scripts import reader_gc_retirement as retirement, reader_image_history_catalog as catalog
from scripts import reader_lifecycle as lifecycle

KEY = 'Owner/Repo\0历史/h.PNG'
OTHER = 'Owner/Repo\0other.PNG'
BUCKET = retirement.ASSETS
WEBP = f"pages/image/png/{'a' * 64}/document.webp"
AVIF = f"pages/image/png/{'b' * 64}/document.avif"
EXTRA = f"pages/image/png/{'a' * 64}/metadata.json"
OLD = '1' * 32
PAST = '2' * 32
RETIRED = '3' * 32
BEFORE = '2026-11-20T00:00:00+00:00'
NOW = '2026-11-21T00:00:00+00:00'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def packed(value):
    return gzip.compress(retirement.encode(value), mtime=0)


def route(path):
    return {'s': 2, 'm': 'i', 'b': BUCKET, 'p': path}


def ref(path):
    return {'bucket': BUCKET, 'path': path}


class TinyChunks:
    def __init__(self, raw):
        self.stream = io.BytesIO(raw)

    def read(self, size):
        assert size >= 0
        return self.stream.read(min(size, 3))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.before = {'v': 1, 'f': {KEY: route(WEBP), OTHER: route(EXTRA)}, 'future': [1, True]}
        self.after = copy.deepcopy(self.before)
        self.after['f'][KEY] = route(AVIF)
        # Use the real lifecycle schema, with history that the regular lifecycle
        # compactor would discard, and an additional non-sidecar bundle member.
        value, _ = lifecycle.catalog_generation(self.before, now=BEFORE)
        current = next(iter(value['generations'].values()))
        current.update(generation=OLD, acks={'hf': True, 'pages': True},
                       snapshot={'bucket': BUCKET, 'path': 'historical', 'other': ['未知']},
                       processing_roots=['reader-index/processing/old.json'])
        current['references'].insert(0, ref(EXTRA + '.aux'))
        self.value = {'generations': {PAST: {**current, 'generation': PAST, 'status': 'superseded',
                                              'superseded_at': BEFORE,
                                              'replacement_acks': {'hf': True, 'pages': True}}, OLD: current},
                      'retired_generations': {RETIRED: {'opaque': {'x.y': [None, True, '历史']}}},
                      'unknown.with.dots': {'generations': {'current': ['payload']}},
                      'current': {'generation': OLD, 'published_at': BEFORE, 'opaque': 42},
                      'kind': 'reader-catalog', 'version': 1}

    def raw(self):
        return (json.dumps(self.value, ensure_ascii=False, indent=2) + '\n \t\n').encode('utf-8')

    def plan(self, raw=None, target=None, **opts):
        raw = self.raw() if raw is None else raw
        target = io.BytesIO() if target is None else target
        args = dict(key=KEY, webp_path=WEBP, avif_path=AVIF,
                    before_sidecar=packed(self.before), after_sidecar=packed(self.after), now=NOW)
        result = catalog.plan(TinyChunks(raw), target, **(args | opts))
        return result, target.getvalue()

    def reject(self, pattern, **opts):
        target = io.BytesIO()
        with self.assertRaisesRegex(ValueError, pattern):
            self.plan(target=target, **opts)
        self.assertEqual(target.getvalue(), b'')

    def test_preserves_history_and_exact_identities_without_compaction(self):
        with patch.object(lifecycle, 'compact_catalog', side_effect=AssertionError('not a compactor')):
            result, raw = self.plan(expected_parent_sha256=sha(self.raw()))
        output = json.loads(raw)
        successor = result['generation']
        entry = output['generations'][successor]
        explicit = [ref(AVIF), ref(EXTRA), ref(EXTRA + '.aux')]
        _, expected = lifecycle.catalog_generation(self.after, self.value, references=explicit, now=NOW)
        self.assertEqual(successor, expected)
        self.assertEqual(result['parent_sha256'], sha(self.raw()))
        self.assertEqual(result['parent_bytes'], len(self.raw()))
        self.assertEqual(result['target_sha256'], sha(raw))
        self.assertEqual(result['target_bytes'], len(raw))
        self.assertEqual(result['before_sidecar_sha256'], self.value['generations'][OLD]['sidecar_sha256'])
        self.assertEqual(entry['parent_generation'], OLD)
        self.assertEqual(entry['sidecar_sha256'], result['after_sidecar_sha256'])
        self.assertEqual(entry['acks'], {'hf': False, 'pages': False})
        self.assertCountEqual(entry['references'], explicit)
        self.assertEqual(entry['snapshot'], result['snapshot'])
        self.assertEqual(result['snapshot']['sha256'], sha(packed(self.after)))
        self.assertEqual(result['snapshot']['bytes'], len(packed(self.after)))
        self.assertEqual(result['snapshot']['path'], f'reader-index/generations/{successor}/reader_assets.json.gz')
        self.assertLess(raw.index(b'"current"'), raw.index(b'"generations"'))
        self.assertEqual(output['current'], {'generation': successor, 'published_at': NOW, 'opaque': 42})
        output['generations'].pop(successor)
        expected = copy.deepcopy(self.value)
        expected['current'] = output['current']
        expected['generations'][OLD].update(status='superseded', superseded_at=NOW)
        self.assertEqual(output, expected)
        hint_path, hint = result['retirement_hint']
        self.assertEqual(retirement.validate(hint_path, hint)['removed'], [ref(WEBP)])
        self.assertEqual(hint['roots'], [{'bucket': BUCKET, 'root': WEBP.rsplit('/', 1)[0]}])
        self.assertFalse(result['deletion_authorized'])

    def test_webp_route_alias_keeps_the_reference_and_has_no_hint(self):
        self.before['f'][OTHER] = self.after['f'][OTHER] = route(WEBP)
        self.value['generations'][OLD]['sidecar_sha256'] = sha(retirement.encode(self.before).rstrip(b'\n'))
        result, raw = self.plan()
        self.assertIsNone(result['retirement_hint'])
        self.assertIn(ref(WEBP), json.loads(raw)['generations'][result['generation']]['references'])

    def test_parent_sidecar_or_route_drift_fails_closed(self):
        self.reject('parent digest', expected_parent_sha256='f' * 64)
        self.reject('exact WebP and AVIF', avif_path=AVIF.replace('b' * 64, 'c' * 64))
        self.after['f'][OTHER]['p'] = WEBP
        self.reject('outside the admitted')
        self.after['f'][OTHER]['p'] = EXTRA
        self.after['future'] = [1, 1]
        self.reject('outside the admitted')
        self.after['future'] = [1, True]
        inexact = retirement.encode(self.after).replace(b'[1,true]', b'[1.0000000000000001,true]')
        self.reject('loses precision', after_sidecar=gzip.compress(inexact))
        self.value['generations'][OLD]['sidecar_sha256'] = sha(packed(self.before))
        self.reject('bind the before-sidecar')

    def test_pending_duplicate_malformed_and_collision_fail_closed(self):
        raw = self.raw()
        self.reject('pending', raw=raw.replace(b'"version": 1', b'"redirect_sidecar_pending": null, "version": 1'))
        self.reject('duplicate', raw=raw.replace(b'"version": 1', b'"version": 1, "version": 1'))
        self.reject('duplicate', raw=raw.replace(b'"status": "current"', b'"status": "current", "status": "current"'))
        self.reject('duplicate', raw=raw.replace(f'"{OLD}":'.encode(), f'"{PAST}":'.encode()))
        self.reject('malformed', raw=raw.rstrip()[:-1])
        self.reject('malformed', raw=raw + b'{}')
        self.reject('duplicate sidecar', before_sidecar=gzip.compress(b'{"v":1,"f":{},"f":{}}'))
        self.reject('surrogate escape', raw=raw.replace(b'"opaque": 42', rb'"opaque": "\ud800"'))
        successor = self.plan()[0]['generation']
        self.value['retired_generations'][successor] = {}
        self.reject('already exists')
        del self.value['retired_generations'][successor]
        self.value['generations'][OLD]['gc_stub'] = True
        self.reject('incomplete catalog generation')

    def test_bounds_before_target_write_and_preserves_opaque_decimals(self):
        raw = self.raw()
        target_size = self.plan()[0]['target_bytes']
        self.reject('source exceeds', max_source_bytes=len(raw) - 1)
        self.reject('target exceeds', max_target_bytes=target_size - 1)
        self.reject('entry exceeds', max_entry_bytes=80)
        self.assertEqual(self.plan(max_source_bytes=len(raw), max_target_bytes=target_size)[0]['target_bytes'],
                         target_size)
        raw = raw.replace(b'"opaque": 42', b'"opaque": 0.123456789123456789123456789')
        _, out = self.plan(raw=raw)
        self.assertEqual(json.loads(out, parse_float=Decimal)['current']['opaque'],
                         Decimal('0.123456789123456789123456789'))

    def test_target_short_write_is_not_a_success(self):
        class ShortTarget(io.BytesIO):
            def write(self, raw):
                return super().write(raw[:-1])

        with self.assertRaisesRegex(OSError, 'short catalog target write'):
            self.plan(target=ShortTarget())


if __name__ == '__main__':
    unittest.main()
