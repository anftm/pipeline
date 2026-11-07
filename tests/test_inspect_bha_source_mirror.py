import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts import inspect_bha_source_mirror as check


def tar_metadata(target, values):
    with tarfile.open(target, 'w:gz') as archive:
        for name, value in values:
            body = json.dumps(value).encode()
            entry = tarfile.TarInfo('repo/' + name); entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))


class SourceMirrorInspectionTests(unittest.TestCase):
    def test_pinned_metadata_counts_manifest_matches_without_downloads_or_writes(self):
        urls = ['https://example.test/a.pdf', 'https://example.test/b.jpg']
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'parsed.tar.gz'
            tar_metadata(target, [('a/a.metadata', {'files': [*urls, urls[0], '/local/path']}),
                                  ('b/b.metadata', {'files': None})])
            selected = check.urls_in_snapshot(target)
        self.assertEqual(selected, set(urls))
        manifest = {'version': 1, 'files': {urls[0]: {'archive_id': 0, 'path': 'archives0/aa/a.pdf',
                                                       'sha256': 'a' * 64, 'bytes': 42}}}
        result = check.compare(0, 'a' * 40, 'b' * 40, json.dumps(manifest).encode(), selected)
        self.assertEqual(result['matched_manifest_urls'], 1)
        self.assertEqual(result['missing_manifest_urls'], 1)
        self.assertEqual(result['source_extensions'], {'.jpg': 1, '.pdf': 1})
        self.assertFalse(result['mirror_requested'])
        self.assertEqual(result['remote_writes'], 0)

    def test_invalid_snapshot_and_manifest_stop_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'parsed.tar.gz'
            for values in ([('a.metadata', {'files': 'bad'})], [('a.metadata', []), ('a.metadata', [])], []):
                tar_metadata(target, values)
                with self.subTest(values=values), self.assertRaises(ValueError): check.urls_in_snapshot(target)
        url = 'https://example.test/a.pdf'
        valid = {'version': 1, 'files': {url: {'archive_id': 0, 'path': 'archives0/aa/a.pdf',
                                               'sha256': 'a' * 64, 'bytes': 42}}}
        for change in ('version', 'hash', 'path', 'size'):
            value = json.loads(json.dumps(valid))
            if change == 'version': value['version'] = 2
            elif change == 'hash': value['files'][url]['sha256'] = 'bad'
            elif change == 'path': value['files'][url]['path'] = 'archives0/../other'
            else: value['files'][url]['bytes'] = 0
            with self.subTest(change=change), self.assertRaises(ValueError):
                check.compare(0, 'a' * 40, 'b' * 40, json.dumps(value).encode(), {url})
        with self.assertRaises(ValueError): check.compare(32, 'a' * 40, 'b' * 40, b'{}', set())
        with self.assertRaises(ValueError): check.compare(0, 'bad', 'b' * 40, b'{}', set())

    def test_remote_inspection_uses_only_pinned_gets_and_flags_moving_heads(self):
        class Api:
            def __init__(self, token): self.token = token; self.revisions = iter(['b' * 40, 'c' * 40]); self.calls = []
            def repo_info(self, *_args, **_kwargs): return type('Info', (), {'sha': next(self.revisions)})()
            def hf_hub_download(self, *args, **kwargs):
                self.calls.append(kwargs['revision']); return manifest_path
        url = 'https://example.test/a.pdf'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); manifest_path = str(root / 'manifest.json')
            Path(manifest_path).write_text(json.dumps({'version': 1, 'files': {}}))
            snapshot = root / 'snapshot.tar.gz'; tar_metadata(snapshot, [('a.metadata', {'files': [url]})])
            with patch.object(check, 'HfApi', Api), \
                    patch.object(check.audit, 'parsed_commit', side_effect=['a' * 40, 'a' * 40]), \
                    patch.object(check.audit, 'download_snapshot', return_value=snapshot):
                result = check.inspect(0, 'github', 'hf')
        self.assertFalse(result['upstream_head_changed_during_check'])
        self.assertTrue(result['mirror_head_changed_during_check'])
        self.assertEqual(result['missing_manifest_urls'], 1)
        self.assertEqual(result['remote_writes'], 0)

    def test_workflow_is_manual_readonly_and_never_invokes_old_mutators(self):
        flow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/bha-source-files.yml').read_text())
        self.assertEqual(set(flow.get('on', flow.get(True))), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        commands = '\n'.join(step.get('run', '') for job in flow['jobs'].values() for step in job['steps'])
        self.assertIn('inspect_bha_source_mirror.py', commands)
        self.assertNotIn('mirror_source_files.py', commands)
        self.assertNotIn('optimize_source_files.py', commands)


if __name__ == '__main__':
    unittest.main()
