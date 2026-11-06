"""Only synthetic cache bytes; no Space, bucket or publishing operations."""

from copy import deepcopy
import gzip
import json
import unittest

from scripts import ccrd_single_source_surfaces as surface


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode()


class SingleSourceSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.old = '旧 文本\n第二行'.encode()
        self.new = '新 文本\n第三行 and more'.encode()
        self.initial = 'data/initial/sources/CW.json'
        self.sidebar = 'data/sidebar/sources/CW.json'
        self.other = 'data/initial/sources/RMRB.json'
        self.folder = 'data/folder_browser.json.gz'
        self.data = {
            self.initial: {'source': 'CW', 'results': [
                {'doc_id': 'CW:0', 'Source': 'CW', 'Size': len(self.old), 'snippet': '旧 文本 第二行', 'extra': 1},
                {'doc_id': 'CW:1', 'Source': 'CW', 'Size': 7, 'snippet': 'keep'}], 'total': 2},
            self.sidebar: {'source': 'CW', 'files': [
                {'doc_id': 'CW:0', 'size': len(self.old), 'name': 'keep name'},
                {'doc_id': 'CW:1', 'size': 7, 'name': 'other'}]},
            self.other: {'source': 'RMRB', 'results': [{'doc_id': 'RMRB:1', 'Size': 42}]},
            self.folder: {'CW': {'': {'files': [{'doc_id': 'CW:0', 'size': len(self.old), 'name': 'keep'},
                                                  {'doc_id': 'CW:1', 'size': 7}], 'folders': []}},
                          'RMRB': {'': {'files': [{'doc_id': 'RMRB:1', 'size': 42}]}}},
        }

    def inventory(self, data=None):
        data = self.data if data is None else data
        payloads = {key: encode(value) for key, value in data.items()}
        payloads[self.folder] = gzip.compress(payloads[self.folder], mtime=0)
        specs = {
            self.initial: {'schema': 'initial-v1', 'sha256': surface.sha(payloads[self.initial]),
                           'positions': [['results', 0]]},
            self.sidebar: {'schema': 'sidebar-v1', 'sha256': surface.sha(payloads[self.sidebar]),
                           'positions': [['files', 0]]},
            self.other: {'schema': 'initial-v1', 'sha256': surface.sha(payloads[self.other]), 'positions': []},
            self.folder: {'schema': 'folder-browser-v1', 'sha256': surface.sha(payloads[self.folder]),
                          'positions': [['CW', '', 'files', 0]]},
        }
        return specs, payloads

    def test_only_target_size_and_snippet_change(self):
        specs, payloads = self.inventory()
        result = surface.update_payloads(specs, payloads, self.old, self.new)
        expected = deepcopy(self.data)
        expected[self.initial]['results'][0].update(Size=len(self.new), snippet='新 文本 第三行 and more')
        expected[self.sidebar]['files'][0]['size'] = len(self.new)
        expected[self.folder]['CW']['']['files'][0]['size'] = len(self.new)
        for name in self.data:
            raw = result['payloads'][name]
            self.assertEqual(json.loads(gzip.decompress(raw) if name.endswith('.gz') else raw), expected[name])
        self.assertEqual(result['payloads'][self.other], payloads[self.other])
        self.assertEqual(result['changed_paths'], sorted([self.initial, self.sidebar, self.folder]))
        self.assertEqual(result['explicit_deletions'], [])
        self.assertEqual(result['remote_writes'], 0)
        self.assertFalse(result['publication_ready'])
        self.assertFalse(result['inventory_complete_proven'])

    def test_unchanged_txt_keeps_all_original_bytes(self):
        specs, payloads = self.inventory()
        result = surface.update_payloads(specs, payloads, self.old, self.old)
        self.assertEqual(result['payloads'], payloads)
        self.assertEqual(result['changed_paths'], [])

    def test_duplicate_keys_and_nonfinite_values_are_rejected_without_rewrite(self):
        specs, payloads = self.inventory()
        for raw in (b'{"results":[],"results":[1]}', b'{"metric":NaN}', b'{"metric":Infinity}'):
            with self.subTest(raw=raw):
                altered_specs, altered_payloads = deepcopy(specs), dict(payloads)
                altered_payloads[self.other] = raw
                altered_specs[self.other]['sha256'] = surface.sha(raw)
                with self.assertRaisesRegex(ValueError, 'invalid JSON'):
                    surface.update_payloads(altered_specs, altered_payloads, self.old, self.new)

    def test_same_size_content_change_updates_snippet_but_not_sidebar_bytes(self):
        replacement = b'X' * len(self.old)
        specs, payloads = self.inventory()
        result = surface.update_payloads(specs, payloads, self.old, replacement)
        self.assertEqual(result['changed_paths'], [self.initial])
        self.assertEqual(result['payloads'][self.sidebar], payloads[self.sidebar])
        self.assertEqual(result['payloads'][self.folder], payloads[self.folder])
        self.assertEqual(json.loads(result['payloads'][self.initial])['results'][0]['snippet'], replacement.decode())

    def test_unknown_occurrence_missing_path_or_wrong_bytes_fails(self):
        specs, payloads = self.inventory()
        for mode in ('unlisted', 'missing', 'stale', 'wrong-schema', 'traversal', 'missing-category', 'bad-digest',
                     'bad-gzip'):
            s, p = deepcopy(specs), deepcopy(payloads)
            if mode == 'unlisted':
                s[self.initial]['positions'] = []
            elif mode == 'missing':
                del p[self.other]
            elif mode == 'stale':
                p[self.sidebar] = p[self.sidebar] + b' '
            elif mode == 'wrong-schema':
                s[self.sidebar]['schema'] = 'unknown-v2'
            elif mode == 'traversal':
                s['data/initial/../outside.json'] = s.pop(self.other)
                p['data/initial/../outside.json'] = p.pop(self.other)
            elif mode == 'bad-digest':
                s[self.other]['sha256'] = None
            elif mode == 'bad-gzip':
                p[self.folder] = b'not gzip'
                s[self.folder]['sha256'] = surface.sha(p[self.folder])
            else:
                s.pop(self.sidebar)
                p.pop(self.sidebar)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                surface.update_payloads(s, p, self.old, self.new)

    def test_incorrect_old_size_snippet_or_schema_fails(self):
        for mode in ('size', 'snippet', 'wrong-source', 'duplicate-positions', 'mislocated'):
            specs, payloads = self.inventory()
            if mode == 'size':
                data = deepcopy(self.data)
                data[self.sidebar]['files'][0]['size'] += 1
                specs, payloads = self.inventory(data)
            elif mode == 'snippet':
                data = deepcopy(self.data)
                data[self.initial]['results'][0]['snippet'] = 'stale'
                specs, payloads = self.inventory(data)
            elif mode == 'wrong-source':
                data = deepcopy(self.data)
                data[self.initial]['results'][0]['Source'] = 'RMRB'
                specs, payloads = self.inventory(data)
            elif mode == 'duplicate-positions':
                specs[self.initial]['positions'].append(['results', 0])
            else:
                specs[self.initial]['positions'] = [['results', 1]]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                surface.update_payloads(specs, payloads, self.old, self.new)


if __name__ == '__main__':
    unittest.main()
