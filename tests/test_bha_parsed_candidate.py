"""Bounded local tree comparison never treats Git identity as deployment proof."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import bha_parsed_candidate as candidate


class BhaParsedCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.git_dir = self.repo / '.git'
        self.write('pub/publication/publication.metadata', '{"title":"book"}')
        self.write('pub/publication/abc/abcdef1234.json', '{"title":"article","parts":[]}')
        self.write('pub/publication/abc/abcdef1234.tags', '[]')
        self.parent = self.tree()

    def write(self, path, value):
        target = self.repo / path; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding='utf8')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def tree(self):
        self.git('add', '-A')
        return self.git('write-tree')

    def test_identical_tree_full_bytes_and_invariants(self):
        value = candidate.compare(self.git_dir, archive=0, parent_tree=self.parent, candidate_tree=self.parent)
        self.assertTrue(value['identical']); self.assertEqual(value['parent']['articles'], 1)
        self.assertEqual(value['parent']['publications'], 1); self.assertEqual(value['parent']['files'], 3)
        self.assertEqual(value['remote_writes'], 0); self.assertFalse(value['consumer_verified'])
        self.assertFalse(value['publication_ready']); self.assertFalse(value['external_writers_verified'])
        self.assertEqual(value['added'] + value['removed'] + value['changed'], 0)

    def test_changed_added_and_removed_paths_are_counted(self):
        self.write('pub/publication/abc/abcdef1234.json', '{"title":"changed"}')
        (self.repo / 'pub/publication/abc/abcdef1234.tags').unlink()
        (self.repo / 'pub/publication/abc/abcdef1234.json').rename(self.repo / 'pub/publication/abc/abcdef1235.json')
        # New article belongs under its own prefix (the prefix remains abc).
        self.write('pub/publication/abc/abcdef1235.tags', '[]')
        current = self.tree()
        value = candidate.compare(self.git_dir, archive=0, parent_tree=self.parent, candidate_tree=current)
        self.assertFalse(value['identical']); self.assertEqual(value['added'], 2); self.assertEqual(value['removed'], 2)
        self.assertEqual(value['changed'], 0); self.assertEqual(value['candidate']['articles'], 1)
        self.assertEqual(len(value['diff_sha256']), 64)

    def test_cli_emits_full_changed_path_inventory_with_hash(self):
        self.write('pub/publication/abc/abcdef1234.json', '{"title":"new"}')
        changed = self.tree()
        output, diff = self.root / 'changed-report.json', self.root / 'changed-diff.jsonl'
        self.assertEqual(candidate.main(['--git-dir', str(self.git_dir), '--archive', '0', '--parent-tree', self.parent,
                                         '--candidate-tree', changed, '--output', str(output), '--diff-output', str(diff)]), 0)
        report, raw = json.loads(output.read_text()), diff.read_bytes()
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(report['changed'], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], 'pub/publication/abc/abcdef1234.json')
        self.assertNotEqual(rows[0]['before_sha256'], rows[0]['after_sha256'])
        self.assertEqual(report['diff_sha256'], hashlib.sha256(raw).hexdigest())

    def test_missing_tags_duplicate_json_and_bad_paths_fail_closed(self):
        for path, contents, expected in (
            ('pub/publication/abc/abcdef1234.tags', None, 'missing tags'),
            ('pub/publication/abc/abcdef1234.json', '{"title":1,"title":2}', 'duplicate object'),
            ('pub/publication/abc/abcdef1234.tags', '{}', 'tags are not an array'),
            ('pub/publication/abc/abcdef1234.json', '{"tags":[]}', 'valid article'),
            ('pub/publication/abc/abcdef1234.json', '{"x":NaN}', 'invalid JSON constant'),
        ):
            with self.subTest(expected=expected):
                self.git('read-tree', '--reset', '-u', self.parent)
                if contents is None: (self.repo / path).unlink()
                else: self.write(path, contents)
                tree = self.tree()
                with self.assertRaisesRegex(ValueError, expected):
                    candidate.inspect(self.git_dir, 0, tree)
        self.git('read-tree', '--reset', '-u', self.parent)
        self.write('bad-file.js', 'do not execute')
        with self.assertRaisesRegex(ValueError, 'path'):
            candidate.inspect(self.git_dir, 0, self.tree())

    def test_blob_and_total_bounds_and_unknown_tree(self):
        with patch.object(candidate, 'MAX_FILE', 2), self.assertRaisesRegex(ValueError, 'file size'):
            candidate.inspect(self.git_dir, 0, self.parent)
        with patch.object(candidate, 'MAX_FILES', 2), self.assertRaisesRegex(ValueError, 'file count'):
            candidate.inspect(self.git_dir, 0, self.parent)
        with patch.object(candidate, 'MAX_TOTAL', 4), self.assertRaisesRegex(ValueError, 'total byte'):
            candidate.inspect(self.git_dir, 0, self.parent)
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            candidate.inspect(self.git_dir, 0, 'a' * 40)
        with self.assertRaises(ValueError):
            candidate.inspect(self.git_dir, 0, 'main')

    def test_git_symlink_executable_and_duplicate_doc_identity_are_rejected(self):
        (self.repo / 'pub/publication/abc/abcdef1234.tags').unlink()
        (self.repo / 'pub/publication/abc/abcdef1234.tags').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            candidate.inspect(self.git_dir, 0, self.tree())
        self.git('read-tree', '--reset', '-u', self.parent)
        self.git('update-index', '--chmod=+x', 'pub/publication/abc/abcdef1234.json')
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            candidate.inspect(self.git_dir, 0, self.git('write-tree'))

    def test_alternates_are_rejected_and_git_environment_excludes_credentials(self):
        alternates = self.git_dir / 'objects/info/alternates'
        alternates.write_text('/external/objects\n')
        with self.assertRaisesRegex(ValueError, 'alternates'):
            candidate.inspect(self.git_dir, 0, self.parent)
        alternates.unlink()
        env = candidate._env(self.git_dir)
        for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'HF_TOKEN', 'GIT_CONFIG_COUNT', 'GIT_DIR'):
            self.assertNotIn(key, env)
        self.assertEqual(env['GIT_OBJECT_DIRECTORY'], str(self.git_dir / 'objects'))
        self.assertEqual(env['GIT_ALTERNATE_OBJECT_DIRECTORIES'], '')
        self.assertEqual(env['GIT_NO_LAZY_FETCH'], '1')

    def test_cli_does_not_overwrite_or_write_inside_git_directory(self):
        existing = self.root / 'existing.json'; existing.write_text('protected')
        args = ['--git-dir', str(self.git_dir), '--archive', '0', '--parent-tree', self.parent,
                '--candidate-tree', self.parent]
        diff = self.root / 'diff.jsonl'
        for output in (existing, self.git_dir / 'report.json'):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'outside'):
                candidate.main([*args, '--output', str(output), '--diff-output', str(diff)])
        self.assertEqual(existing.read_text(), 'protected')
        output = self.root / 'report.json'
        self.assertEqual(candidate.main([*args, '--output', str(output), '--diff-output', str(diff)]), 0)
        report = json.loads(output.read_text())
        self.assertTrue(report['identical'])
        self.assertEqual(report['diff_sha256'], hashlib.sha256(diff.read_bytes()).hexdigest())
        self.assertEqual(report['diff_bytes'], diff.stat().st_size)


if __name__ == '__main__':
    unittest.main()
