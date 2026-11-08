"""Streaming CW:0 metadata patch preserves all unrelated values."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts import ccrd_metadata_patch as patcher
from scripts import ccrd_text_features as features

OLD, NEW = '原始 text\n', '更新 after text\n'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    path.write_bytes(gzip.compress(raw, mtime=0) if path.name.endswith('.gz') else raw)


def load(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.name.endswith('.gz') else raw)


def fixture():
    return {
        patcher.PATHS[0]: {'version': 1, 'records': [{'doc_id': 'CW:0', 'size': len(OLD.encode()), 'x': [1, '中文']},
                                                     {'doc_id': 'CW:1', 'size': 4}], 'sources': [{'slug': 'CW'}]},
        patcher.PATHS[1]: {'CW': {'': {'files': [{'name': 'a', 'size': len(OLD.encode()), 'doc_id': 'CW:0'}],
                                         'folders': []}}, 'PLA': {'': {'files': [], 'folders': []}}},
        patcher.PATHS[2]: {'results': [{'doc_id': 'CW:0', 'Size': len(OLD.encode()),
                                       'snippet': features.summarize(OLD), 'nested': {'x': 1}},
                                      {'doc_id': 'CW:1', 'Size': 4}], 'total': 2},
        patcher.PATHS[3]: {'files': [{'doc_id': 'CW:0', 'size': len(OLD.encode()), 'name': 'a'}], 'folders': []},
    }


class CcrdMetadataPatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.inputs = self.root / 'input'; self.inputs.mkdir(); self.output = self.root / 'output'
        self.data = fixture()
        for name, value in self.data.items(): save(self.inputs / name, value)
        self.expected = {name: patcher.identity(self.inputs / name) for name in patcher.PATHS}

    def test_all_non_target_json_values_and_input_bytes_are_preserved(self):
        before = {name: (self.inputs / name).read_bytes() for name in patcher.PATHS}
        result = patcher.build(self.inputs, self.output, expected=self.expected, old_text=OLD, new_text=NEW)
        self.assertFalse(result['publication_ready']); self.assertEqual(result['remote_writes'], 0)
        self.assertTrue(result['all_other_json_values_verified'])
        for name in patcher.PATHS:
            self.assertEqual((self.inputs / name).read_bytes(), before[name])
            item = patcher._selected(self.output / name, name)
            field = 'Size' if name == patcher.PATHS[2] else 'size'
            self.assertEqual(item[field], len(NEW.encode()))
            if name == patcher.PATHS[2]: self.assertEqual(item['snippet'], features.summarize(NEW))
        self.assertEqual(load(self.output / patcher.PATHS[0])['records'][1], self.data[patcher.PATHS[0]]['records'][1])
        self.assertEqual(load(self.output / patcher.PATHS[1])['PLA'], self.data[patcher.PATHS[1]]['PLA'])

    def test_bad_identity_target_size_and_duplicate_fail_before_publication(self):
        self.expected[patcher.PATHS[0]] = {'bytes': 1, 'sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            patcher.build(self.inputs, self.output, expected=self.expected, old_text=OLD, new_text=NEW)
        self.assertFalse(self.output.exists())
        self.expected[patcher.PATHS[0]] = patcher.identity(self.inputs / patcher.PATHS[0])
        altered = fixture()[patcher.PATHS[3]]; altered['files'][0]['size'] = 999
        save(self.inputs / patcher.PATHS[3], altered); self.expected[patcher.PATHS[3]] = patcher.identity(self.inputs / patcher.PATHS[3])
        with self.assertRaisesRegex(ValueError, 'size differs'):
            patcher.build(self.inputs, self.output, expected=self.expected, old_text=OLD, new_text=NEW)
        self.assertFalse(self.output.exists())
        altered['files'].append(dict(altered['files'][0])); save(self.inputs / patcher.PATHS[3], altered)
        self.expected[patcher.PATHS[3]] = patcher.identity(self.inputs / patcher.PATHS[3])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            patcher.build(self.inputs, self.output, expected=self.expected, old_text=OLD, new_text=NEW)

    def test_verifier_rejects_non_target_drift_and_target_mismatch(self):
        patcher.build(self.inputs, self.output, expected=self.expected, old_text=OLD, new_text=NEW)
        name = patcher.PATHS[3]; old_item = self.data[name]['files'][0]
        changed = load(self.output / name); changed['folders'] = [{'unexpected': True}]; save(self.output / name, changed)
        with self.assertRaisesRegex(ValueError, 'non-target values changed'):
            patcher.verify(self.inputs / name, self.output / name, name, old_item, {'size': len(NEW.encode())})

    def test_source_symlink_and_output_inside_input_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside input'):
            patcher.build(self.inputs, self.inputs / 'nested', expected=self.expected, old_text=OLD, new_text=NEW)
        with self.assertRaisesRegex(ValueError, 'outside input'):
            patcher.build(self.inputs, self.root, expected=self.expected, old_text=OLD, new_text=NEW)
        target = self.inputs / patcher.PATHS[3]; real = self.root / 'real.json'; target.rename(real); target.symlink_to(real)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            patcher.build(self.inputs, self.output, expected=self.expected, old_text=OLD, new_text=NEW)


if __name__ == '__main__':
    unittest.main()
