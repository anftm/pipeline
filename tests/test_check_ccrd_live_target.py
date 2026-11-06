"""Synthetic GET-only checks for current target delivery and source counts."""
from copy import deepcopy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_ccrd_live_target as live
from scripts import check_ccrd_txt_only as checker


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


class LiveTargetTests(unittest.TestCase):
    def setUp(self):
        self.txt = '旧的合成正文\n'.encode()
        self.revision = 'a' * 40
        self.generation = 'ccrd-four-' + 'b' * 64
        self.counts = {'CCRD': 2, 'CW': 3, 'RMRB': 4, 'PLA': 5}
        self.manifest = {'generation': self.generation, 'base_metadata_sha256': 'c' * 64,
                         'databases': {name: {'documents': count} for name, count in self.counts.items()}}
        self.report = {'space_revision': self.revision, 'index_generation': self.generation,
                       'index_manifest_sha256': 'd' * 64, 'site_txt_sha256': hashlib.sha256(self.txt).hexdigest(),
                       'upstream_matches_site': True}
        self.observation = {'report': self.report, 'site_txt': self.txt, 'manifest_bytes': encode(self.manifest)}
        self.status = {'ready': True, 'bucket': {'state': 'ready', 'source': 'space-activation',
                       'generation': self.generation, 'base_metadata_sha256': 'c' * 64,
                       'sources': list(self.counts)}}
        self.sources = [{'slug': name, 'count': count} for name, count in self.counts.items()]
        self.preview = {'doc_id': 'CW:0', 'source': 'CW', 'text': self.txt.decode()}
        self.folder = {'files': [{'doc_id': 'CW:0', 'size': len(self.txt)}], 'folders': []}
        self.download = self.txt
        self.api_reads = 0
        self.status_reads = 0
        self.calls = []
        self.drift = False

    def read(self, url, *, limit):
        self.calls.append((url, limit))
        if url.endswith('/api/index-status'):
            self.status_reads += 1
            status = deepcopy(self.status)
            if self.drift and self.status_reads == 2:
                status['bucket']['generation'] = 'ccrd-four-' + 'f' * 64
            return encode(status)
        if url.endswith('/api/sources'):
            return encode(self.sources)
        if url.endswith('/api/download/CW%3A0'):
            return self.download
        if url.endswith('/api/preview/CW%3A0'):
            return encode(self.preview)
        if url.endswith('/api/folders/CW/contents'):
            return encode(self.folder)
        if url == f'https://huggingface.co/api/spaces/{checker.SPACE}':
            self.api_reads += 1
            return encode({'sha': self.revision, 'runtime': {'sha': self.revision, 'stage': 'RUNNING'}})
        raise AssertionError('unexpected fixture endpoint')

    def run_check(self):
        with patch.object(live.base, 'observe', return_value=self.observation):
            return live.check(read=self.read)

    def test_match_checks_full_bytes_folder_and_all_source_counts_without_writes(self):
        result = self.run_check()
        self.assertTrue(result['target_download_matches'])
        self.assertTrue(result['target_preview_matches'])
        self.assertTrue(result['folder_target_size_matches'])
        self.assertTrue(result['four_source_counts_match'])
        self.assertFalse(result['fulltext_query_verified'])
        self.assertFalse(result['all_index_bytes_verified'])
        self.assertFalse(result['external_writers_fenced'])
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(self.status_reads, 2)
        self.assertEqual(self.api_reads, 1)
        self.assertTrue(all(url.startswith(('https://vomebook-search.hf.space/', 'https://huggingface.co/'))
                            for url, _limit in self.calls))

    def test_mismatch_or_changed_status_fails_closed(self):
        for mode in ('download', 'preview', 'preview-id', 'folder', 'folder-duplicate', 'missing-source',
                     'duplicate-source', 'wrong-count', 'manifest-count-type', 'status-drift', 'space-drift'):
            self.setUp()
            if mode == 'download':
                self.download = b'changed'
            elif mode == 'preview':
                self.preview['text'] = 'changed'
            elif mode == 'preview-id':
                self.preview['doc_id'] = 'CW:1'
            elif mode == 'folder':
                self.folder['files'][0]['size'] += 1
            elif mode == 'folder-duplicate':
                self.folder['files'].append(deepcopy(self.folder['files'][0]))
            elif mode == 'missing-source':
                self.sources.pop()
            elif mode == 'duplicate-source':
                self.sources[-1] = deepcopy(self.sources[0])
            elif mode == 'wrong-count':
                self.sources[0]['count'] += 1
            elif mode == 'manifest-count-type':
                self.manifest['databases']['CW']['documents'] = True
                self.observation['manifest_bytes'] = encode(self.manifest)
            elif mode == 'status-drift':
                self.drift = True
            else:
                self.report['space_revision'] = 'f' * 40
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.run_check()

    def test_upstream_difference_is_reported_without_upload(self):
        self.report['upstream_matches_site'] = False
        result = self.run_check()
        self.assertFalse(result['upstream_matches_site'])
        self.assertEqual(result['remote_writes'], 0)
        with patch.object(live, 'check', return_value=result), patch('sys.argv', ['check']):
            self.assertEqual(live.main(), 2)

    def test_imported_live_check_is_not_scheduled_or_called_by_workflow(self):
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        self.assertFalse(any('check_ccrd_live_target' in path.read_text() for path in root.glob('*.yml')))


if __name__ == '__main__':
    unittest.main()
