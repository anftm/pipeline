import json
import unittest
from types import SimpleNamespace

from scripts import search_gc as gc, search_gc_roots as roots
from tests.test_search_gc import MemoryStore, NOW


class RootsTests(unittest.TestCase):
    def fixtures(self):
        a, b = 'a' * 40, 'b' * 40
        g = '1' * 64
        response = lambda value: SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
        session = SimpleNamespace(get=lambda url, **kw: response(
            {'object': {'sha': a}} if url.endswith('/main') else {'name': 'SEARCH_DATA_GENERATION', 'value': g}))
        api = SimpleNamespace(space_info=lambda *a, **k: SimpleNamespace(sha=b),
            get_space_variables=lambda *a: {'SEARCH_DATA_GENERATION': SimpleNamespace(value=g)})
        def get(url):
            if url.endswith('/Dockerfile'):
                return ('ARG SEARCH_DATA_GENERATION=' + '2' * 64 + '\n').encode()
            if url.endswith('/prepare_test_data.mjs'):
                return ('export const DEFAULT_GENERATION = "' + '3' * 64 + '";\n').encode()
            if url.endswith('/config/search-data.json'):
                return json.dumps({'generation': '4' * 64}).encode()
            if url.endswith('/api/search-data-generation'):
                return json.dumps({'generation': g, 'surface': 'space', 'verified': True}).encode()
            return json.dumps({'generation': g, 'version': 1, 'kind': 'search-snapshot-local'}).encode()
        return session, api, get

    def test_current_exact_build_and_test_pins_and_live_defaults(self):
        session, api, get = self.fixtures()
        seen = []
        def capture(url):
            seen.append(url)
            return get(url)
        result = roots.collect(session=session, api=api, get=capture)
        self.assertEqual(set(result.values()), {'1' * 64, '2' * 64, '3' * 64, '4' * 64})
        self.assertTrue(any('/' + 'a' * 40 + '/config/' in url for url in seen))
        self.assertTrue(any('/resolve/' + 'b' * 40 + '/Dockerfile' in url for url in seen))

    def test_unknown_config_and_http_failure_never_become_empty_roots(self):
        session, api, get = self.fixtures()
        for replacement in [b'ARG SEARCH_DATA_GENERATION=""\n', b'ARG SEARCH_DATA_GENERATION=' + b'2' * 64 + b'\nARG SEARCH_DATA_GENERATION=' + b'3' * 64 + b'\n']:
            with self.assertRaises(ValueError):
                roots.collect(session=session, api=api, get=lambda u: replacement if u.endswith('/Dockerfile') else get(u))
        with self.assertRaises(OSError):
            roots.collect(session=session, api=api, get=lambda u: (_ for _ in ()).throw(OSError('network')))

    def test_revision_drift_rejected(self):
        session, api, get = self.fixtures()
        revisions = iter(['b' * 40, 'c' * 40])
        api.space_info = lambda *a, **k: SimpleNamespace(sha=next(revisions))
        with self.assertRaisesRegex(ValueError, 'changed'):
            roots.collect(session=session, api=api, get=get)

    def test_old_but_live_build_pin_survives_mark_and_sweep(self):
        s = MemoryStore()
        old = s.generation('old', 30 * gc.DAY)
        prev = s.generation('prev', 20 * gc.DAY)
        current = s.generation('current', 19 * gc.DAY)
        s.generation('candidate1', 10 * gc.DAY)
        s.generation('candidate2', 9 * gc.DAY)
        s.publish(current, prev)
        mark = gc.mark(s, run_id='100', now=NOW, known_writers_coordinated=True)
        self.assertIn(old, mark['plan']['manifests'])
        s.consumer_roots = lambda: {'build-pin': old.split('/')[1]}
        s.now += gc.MARK_DELAY
        result = gc.sweep(s, plan_key=mark['plan_key'], run_id='200', now=s.now, known_writers_coordinated=True)
        self.assertIn(old, result['cancelled_manifests'])
        self.assertIn(old, s.files)
        self.assertIn('consumer-build-pin', gc.graph(s, s.now)['retained_generations'][old])

    def test_missing_consumer_generation_and_changed_roots_fail_closed(self):
        s = MemoryStore()
        prev = s.generation('prev', 30 * gc.DAY)
        current = s.generation('current', 20 * gc.DAY)
        s.publish(current, prev)
        s.consumer_roots = lambda: {'pin': 'f' * 64}
        with self.assertRaisesRegex(ValueError, 'consumer manifest'):
            gc.plan(s, run_id='1', now=NOW)
        observations = iter([{'pin': current.split('/')[1]}, {'pin': prev.split('/')[1]}])
        s.consumer_roots = lambda: next(observations)
        with self.assertRaisesRegex(ValueError, 'roots changed'):
            gc.plan(s, run_id='1', now=NOW)
        self.assertEqual(s.deletes, [])


if __name__ == '__main__':
    unittest.main()
