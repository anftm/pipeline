"""Synthetic bounded search-query tests; no production query is sent."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.request import Request

from scripts import check_ccrd_live_search_sample as search
from scripts import check_ccrd_live_target as live
from scripts import check_ccrd_txt_only as checker


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


class SearchSampleTests(unittest.TestCase):
    def setUp(self):
        self.txt = '合成第一句，目标关键词。另一句。'.encode()
        self.revision = 'a' * 40
        self.generation = 'ccrd-four-' + 'b' * 64
        self.manifest = {'generation': self.generation, 'base_metadata_sha256': 'c' * 64}
        self.manifest_raw = encode(self.manifest)
        self.report = {'space_revision': self.revision, 'index_generation': self.generation,
                       'index_manifest_sha256': hashlib.sha256(self.manifest_raw).hexdigest(),
                       'site_txt_sha256': hashlib.sha256(self.txt).hexdigest()}
        self.observation = {'report': self.report, 'site_txt': self.txt, 'manifest_bytes': self.manifest_raw}
        self.status = {'ready': True, 'bucket': {'state': 'ready', 'source': 'space-activation',
                       'generation': self.generation, 'base_metadata_sha256': 'c' * 64,
                       'sources': ['CCRD', 'CW', 'RMRB', 'PLA']}}
        self.rows = [{'doc_id': 'CW:1'}, {'doc_id': 'CW:0'}]
        self.calls = []
        self.status_reads = 0
        self.drift = False
        self.result_change = None

    def read(self, url, *, limit):
        if url.endswith('/api/index-status'):
            self.status_reads += 1
            state = deepcopy(self.status)
            if self.drift and self.status_reads == 2:
                state['bucket']['generation'] = 'ccrd-four-' + 'd' * 64
            return encode(state)
        if url == f'https://huggingface.co/api/spaces/{checker.SPACE}':
            return encode({'sha': self.revision, 'runtime': {'sha': self.revision, 'stage': 'RUNNING'}})
        raise AssertionError('unexpected GET')

    def post(self, payload):
        self.calls.append(deepcopy(payload))
        start = (payload['page'] - 1) * payload['page_size']
        value = {'indexing': False, 'total': len(self.rows), 'index_generation': 4, 'page': payload['page'],
                 'page_size': payload['page_size'], 'results': self.rows[start:start + payload['page_size']]}
        if self.result_change:
            self.result_change(value, payload)
        return encode(value)

    def run_sample(self, phrase='目标关键词', present=True):
        return search.check(observation=self.observation, phrase=phrase, expect_present=present,
                            read=self.read, post=self.post)

    def test_positive_and_negative_queries_are_bounded_and_read_only(self):
        result = self.run_sample()
        self.assertTrue(result['target_match_observed'])
        self.assertFalse(result['publication_ready'])
        self.assertFalse(result['full_index_semantics_verified'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(self.calls[0], {'q': '目标关键词', 'page': 1, 'page_size': 200, 'exact': True,
                                         'search_paths': False, 'fulltext': True})
        self.rows = []
        result = self.run_sample(phrase='不存在的合成词', present=False)
        self.assertFalse(result['target_match_observed'])
        self.assertTrue(result['all_results_checked'])

    def test_two_character_query_uses_same_bounded_verification(self):
        result = self.run_sample(phrase='目标')
        self.assertTrue(result['target_match_observed'])
        self.assertEqual(self.calls[0]['q'], '目标')

    def test_later_page_target_and_bounded_negative_absence(self):
        self.rows = [{'doc_id': f'CW:{n}'} for n in range(1, 250)] + [{'doc_id': 'CW:0'}]
        result = self.run_sample()
        self.assertEqual(result['pages_checked'], 2)
        self.rows = [{'doc_id': f'CW:{n}'} for n in range(1, 802)]
        with self.assertRaisesRegex(ValueError, 'cannot be established'):
            self.run_sample(phrase='不存在的合成词', present=False)
        self.assertEqual(self.calls[-1]['page'], search.MAX_PAGES)

    def test_query_and_snapshot_mismatch_are_refused_before_post(self):
        for phrase, present in [('不存在的合成词', True), ('目标关键词', False), ('*目标*', True), ('a', True)]:
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                self.run_sample(phrase, present)
        self.assertEqual(self.calls, [])
        self.report['index_manifest_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.run_sample()
        self.assertEqual(self.calls, [])

    def test_bad_pagination_or_drifting_state_fails(self):
        for mode in ('indexing', 'page', 'generation-type', 'incomplete', 'duplicate', 'status-drift'):
            self.setUp()
            if mode == 'status-drift':
                self.drift = True
            elif mode == 'duplicate':
                self.rows = [{'doc_id': 'CW:0'}, {'doc_id': 'CW:0'}]
            else:
                def change(value, _payload):
                    if mode == 'indexing':
                        value['indexing'] = True
                    elif mode == 'page':
                        value['page'] = 2
                    elif mode == 'generation-type':
                        value['index_generation'] = True
                    else:
                        value['total'] += 1
                self.result_change = change
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.run_sample()

    def test_multi_page_runtime_generation_change_fails(self):
        self.rows = [{'doc_id': f'CW:{n}'} for n in range(1, 250)] + [{'doc_id': 'CW:0'}]
        self.result_change = lambda value, payload: value.update(index_generation=5 if payload['page'] == 2 else 4)
        with self.assertRaisesRegex(ValueError, 'changed during paging'):
            self.run_sample()

    def test_negative_query_does_not_stop_after_first_page(self):
        self.rows = [{'doc_id': f'CW:{n}'} for n in range(1, 250)] + [{'doc_id': 'CW:0'}]
        with self.assertRaisesRegex(ValueError, 'membership differs'):
            self.run_sample(phrase='不存在的合成词', present=False)
        self.assertEqual([call['page'] for call in self.calls], [1, 2])

    def test_positive_target_absent_from_complete_results_fails(self):
        self.rows = [{'doc_id': 'CW:2'}]
        with self.assertRaisesRegex(ValueError, 'membership differs'):
            self.run_sample()

    def test_transport_is_fixed_post_without_credentials_or_redirects(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, limit):
                self.limit = limit
                return b'{}'

        with patch.object(search, 'build_opener') as opener:
            opener.return_value.open.return_value = Response()
            self.assertEqual(search.post_search({'q': 'fixture'}), b'{}')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, search.URL)
            self.assertEqual(request.get_method(), 'POST')
            self.assertFalse(request.has_header('Authorization'))
        handler = search.NoRedirects()
        with self.assertRaises(RuntimeError):
            handler.redirect_request(Request(search.URL), None, 302, '', {}, 'https://example.org/')
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        self.assertFalse(any('check_ccrd_live_search_sample' in file.read_text() for file in workflows.glob('*.yml')))


if __name__ == '__main__':
    unittest.main()
