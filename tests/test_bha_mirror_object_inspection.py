"""BHA mirror metadata and selected bytes require fixed dataset revision."""
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import bha_mirror_object_inspection as subject

RAW = b'synthetic PDF bytes'
DIGEST = hashlib.sha256(RAW).hexdigest()
PATH = f'archives0/{DIGEST[:2]}/{DIGEST}.pdf'
REV = 'a' * 40


def fixture():
    entry = {'archive_id': 0, 'path': PATH, 'sha256': DIGEST, 'bytes': len(RAW)}
    obj = SimpleNamespace(path=PATH, size=len(RAW), lfs=SimpleNamespace(sha256=DIGEST, size=len(RAW)))
    api = Mock(); api.get_paths_info.return_value = [obj]; api.repo_info.return_value = SimpleNamespace(sha=REV)
    return {'version': 1, 'files': {'https://example.invalid/source': entry}}, api


class MirrorObjectsTests(unittest.TestCase):
    def test_metadata_and_bounded_full_bytes_are_checked(self):
        manifest, api = fixture()
        result = subject.inspect(api, archive=0, revision=REV, manifest=manifest, fetch=lambda *_: RAW)
        self.assertEqual(result['objects'], 1); self.assertEqual(result['metadata_verified'], 1)
        self.assertEqual(result['fully_downloaded_verified'], [PATH]); self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(api.get_paths_info.call_args.kwargs['revision'], REV)

    def test_mismatch_missing_or_stale_dataset_fails_closed(self):
        manifest, api = fixture(); api.get_paths_info.return_value[0].lfs.sha256 = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'metadata differs'):
            subject.inspect(api, archive=0, revision=REV, manifest=manifest, fetch=lambda *_: RAW)
        manifest, api = fixture(); api.get_paths_info.return_value = []
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            subject.inspect(api, archive=0, revision=REV, manifest=manifest, fetch=lambda *_: RAW)
        manifest, api = fixture(); api.repo_info.return_value.sha = 'b' * 40
        with self.assertRaisesRegex(RuntimeError, 'dataset changed'):
            subject.inspect(api, archive=0, revision=REV, manifest=manifest, fetch=lambda *_: RAW)
        manifest, api = fixture()
        with self.assertRaisesRegex(ValueError, 'downloaded bytes differ'):
            subject.inspect(api, archive=0, revision=REV, manifest=manifest, fetch=lambda *_: b'wrong')

    def test_workflow_is_manual_readonly_with_independent_concurrency_group(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-mirror-object-check.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'bha-mirror-object-check')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['full_samples']['default'], '1')
        commands = '\n'.join(item.get('run', '') for item in flow['jobs']['inspect']['steps'])
        self.assertIn('scripts/bha_mirror_object_inspection.py', commands)
        for forbidden in ('mirror_source_files.py', 'optimize_source_files.py', 'git push'):
            self.assertNotIn(forbidden, commands)

    def test_invalid_scope_or_missing_archive_is_not_an_empty_success(self):
        manifest, api = fixture()
        for archive, limit in ((32, 0), (True, 0), (0, 5)):
            with self.subTest(archive=archive, limit=limit), self.assertRaises(ValueError):
                subject.inspect(api, archive=archive, revision=REV, manifest=manifest, full_limit=limit)
        with self.assertRaisesRegex(ValueError, 'no selected archive'):
            subject.inspect(api, archive=1, revision=REV, manifest=manifest)


if __name__ == '__main__': unittest.main()
