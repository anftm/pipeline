"""Offline Pages trigger checks with disposable local Git repositories."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish_search_reader_index as publication


IDENTITY = {
    'GIT_AUTHOR_NAME': 'vomebook', 'GIT_AUTHOR_EMAIL': 'vomebook@gmail.com',
    'GIT_COMMITTER_NAME': 'vomebook', 'GIT_COMMITTER_EMAIL': 'vomebook@gmail.com',
}


class PagesTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pages-trigger-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # No ambient credentials, Git config, hooks or identity are needed.
        self.environment = patch.dict(os.environ, {
            'PATH': os.environ['PATH'], 'HOME': str(self.root),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            **IDENTITY,
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.source = self.root / 'reader.gz'
        self.git('init', '--bare', '--initial-branch=main', str(self.remote))
        self.git('init', '--initial-branch=main', str(self.seed))
        (self.seed / '.gitignore').write_text('/data/\n')
        (self.seed / 'unrelated.txt').write_text('keep current main\n')
        (self.seed / '.github').mkdir()
        (self.seed / '.github' / 'keep.json').write_text('{}\n')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'initial', cwd=self.seed)
        self.git('remote', 'add', 'origin', str(self.remote), cwd=self.seed)
        self.git('push', '-u', 'origin', 'main', cwd=self.seed)
        # main must be selected even when the remote default branch differs.
        self.git('branch', 'old-default', 'main', cwd=self.remote)
        self.git('symbolic-ref', 'HEAD', 'refs/heads/old-default', cwd=self.remote)
        self.initial = self.head()
        self.calls = []
        self.real_run = publication.run
        self.runner = patch.object(publication, 'run', side_effect=self.local_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.sidecar = {'v': 1, 'f': {'fixture/book.txt': ['t', '正文']}}
        self.pack(self.sidecar)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def head(self):
        return self.git('rev-parse', 'main', cwd=self.remote)

    def pack(self, value, *, mtime=0, compresslevel=9, indent=None, ensure_ascii=False):
        self.source.write_bytes(gzip.compress(json.dumps(value, indent=indent,
            ensure_ascii=ensure_ascii).encode(), mtime=mtime, compresslevel=compresslevel))

    def local_run(self, args, cwd=None, env=None):
        self.calls.append(list(args))
        if args[:2] == ['git', 'clone']:
            args = [*args[:-2], self.remote.as_uri(), args[-1]]
        return self.real_run(args, cwd=cwd, env=env)

    def publish(self):
        publication.publish_to_pages(self.source, 'fixture/site', 'fixture-token', dry_run=False)

    def marker(self):
        return json.loads(self.git('show', 'main:' + publication.REFRESH_PATH, cwd=self.remote))

    def test_ignored_data_identity_normalization_and_changed_content(self):
        with patch.dict(os.environ, {
            'GIT_AUTHOR_NAME': 'wrong', 'GIT_AUTHOR_EMAIL': 'wrong@example.test',
            'GIT_COMMITTER_NAME': 'wrong', 'GIT_COMMITTER_EMAIL': 'wrong@example.test',
        }):
            self.publish()
        first = self.head()
        self.assertNotEqual(first, self.initial)
        self.assertEqual(self.git('show', '-s', '--format=%an <%ae>|%cn <%ce>', 'main', cwd=self.remote),
                         'vomebook <vomebook@gmail.com>|vomebook <vomebook@gmail.com>')
        expected_sha = hashlib.sha256(json.dumps(self.sidecar, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(self.marker(), {'sidecar_schema': 1, 'sidecar_sha256': expected_sha})
        self.assertEqual(self.git('diff', '--name-only', self.initial, first, cwd=self.remote),
                         publication.REFRESH_PATH)
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', 'main', '--', 'data', cwd=self.remote), '')
        self.assertEqual(self.git('show', 'main:.gitignore', cwd=self.remote), '/data/')
        self.assertEqual(self.git('show', 'main:unrelated.txt', cwd=self.remote), 'keep current main')
        self.assertEqual(self.git('show', 'main:.github/keep.json', cwd=self.remote), '{}')
        self.calls.clear()
        self.publish()
        self.assertEqual(self.head(), first)
        original_gzip = self.source.read_bytes()
        self.pack(self.sidecar, mtime=123, compresslevel=1)
        self.assertNotEqual(self.source.read_bytes(), original_gzip)
        self.publish()
        self.assertEqual(self.head(), first)
        self.pack({'f': self.sidecar['f'], 'v': 1}, indent=4, ensure_ascii=True)
        self.publish()
        self.assertEqual(self.head(), first)
        self.assertFalse(any(args[:2] in (['git', 'commit'], ['git', 'push']) for args in self.calls))
        self.sidecar['f']['fixture/book.txt'] = ['t', 'changed']
        self.pack(self.sidecar)
        self.publish()
        self.assertNotEqual(self.head(), first)
        self.assertNotEqual(self.marker()['sidecar_sha256'], expected_sha)
        self.assertEqual(self.git('rev-list', '--count', 'main', cwd=self.remote), '3')
        self.assertEqual(self.git('diff', '--name-only', first, 'main', cwd=self.remote), publication.REFRESH_PATH)

    def test_optional_v3_generation_and_legacy_tracked_data_preserved(self):
        (self.seed / 'data').mkdir()
        (self.seed / 'data' / 'reader_assets.json.gz').write_bytes(b'legacy-data')
        self.git('add', '-f', 'data/reader_assets.json.gz', cwd=self.seed)
        self.git('commit', '-m', 'legacy site fixture', cwd=self.seed)
        self.git('push', cwd=self.seed)
        self.sidecar['v3'] = {'generation': 'a' * 64}
        self.pack(self.sidecar)
        self.publish()
        self.assertEqual(self.marker()['v3_generation'], 'a' * 64)
        self.assertEqual(self.git('show', 'main:data/reader_assets.json.gz', cwd=self.remote), 'legacy-data')

    def test_lost_push_response_retries_current_main_without_duplicate_commit(self):
        lost = False
        def lose_response(args, cwd=None, env=None):
            nonlocal lost
            result = self.local_run(args, cwd=cwd, env=env)
            if args[:2] == ['git', 'push'] and not lost:
                self.assertEqual(result[0], 0)
                lost = True
                return 1, '', 'connection reset'
            return result
        with patch.object(publication, 'run', side_effect=lose_response), patch.object(publication.time, 'sleep') as sleep:
            self.publish()
        sleep.assert_called_once_with(2)
        self.assertEqual(sum(args[:2] == ['git', 'clone'] for args in self.calls), 2)
        self.assertEqual(sum(args[:2] == ['git', 'push'] for args in self.calls), 1)
        self.assertEqual(self.git('rev-list', '--count', 'main', cwd=self.remote), '2')

    def test_non_fast_forward_retries_and_preserves_concurrent_main_change(self):
        advanced = False
        def advance_main(args, cwd=None, env=None):
            nonlocal advanced
            if args[:2] == ['git', 'push'] and not advanced:
                (self.seed / 'unrelated.txt').write_text('concurrent main change\n')
                self.git('add', 'unrelated.txt', cwd=self.seed)
                self.git('commit', '-m', 'concurrent update', cwd=self.seed)
                self.git('push', cwd=self.seed)
                advanced = True
            return self.local_run(args, cwd=cwd, env=env)
        with patch.object(publication, 'run', side_effect=advance_main), patch.object(publication.time, 'sleep') as sleep:
            self.publish()
        sleep.assert_called_once_with(2)
        self.assertEqual(sum(args[:2] == ['git', 'clone'] for args in self.calls), 2)
        self.assertEqual(self.git('show', 'main:unrelated.txt', cwd=self.remote), 'concurrent main change')
        self.assertEqual(self.git('rev-list', '--count', 'main', cwd=self.remote), '3')

    def test_dry_run_never_reads_source_or_runs_git(self):
        self.source.unlink()
        publication.publish_to_pages(self.source, '', '', dry_run=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.head(), self.initial)

    def test_invalid_schema_or_generation_fails_before_git(self):
        for value in ([], {}, {'v': True, 'f': {}}, {'v': 2, 'f': {}}, {'v': 1, 'f': []},
                      {'v': 1, 'f': {}, 'v3': []}, {'v': 1, 'f': {}, 'v3': {'generation': 'bad'}},
                      {'v': 1, 'f': {'bad': float('nan')}}):
            with self.subTest(value=value):
                self.pack(value)
                with self.assertRaises(ValueError):
                    self.publish()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.head(), self.initial)

    def test_only_transient_errors_retry_and_stop_after_four_attempts(self):
        for message, attempts in (('permission denied', 1), ('http 503', 4)):
            with self.subTest(message=message), \
                    patch.object(publication, '_publish_to_pages_once', side_effect=RuntimeError(message)) as once, \
                    patch.object(publication.time, 'sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, message):
                    self.publish()
                self.assertEqual(once.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)


if __name__ == '__main__':
    unittest.main()
