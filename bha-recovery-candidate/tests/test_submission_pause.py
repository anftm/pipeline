"""Never accept submissions while the central workflow is stopped."""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import document_identity, main, parse_api, proofread_api, source_files


class SubmissionPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_absent_or_unrecognized_pause_setting_stops_before_any_dispatch(self):
        proof = {"doc_id": "offline-proof", "patch": {"version": 2, "parts": {}, "comments": {}}}
        split = {"doc_id": "offline-parse", "articles": [{"title": "文章", "page_start": 1,
                 "page_end": 1, "content": "正文", "base_part_count": 1}]}
        for setting in (None, "", "false", "1", "unexpected"):
            environment = {} if setting is None else {"BHA_PIPELINE_SUBMISSIONS_PAUSED": setting}
            with self.subTest(setting=setting), patch.dict('os.environ', environment, clear=True), \
                    patch.object(proofread_api, 'GITHUB_TOKEN', 'fixture'), \
                    patch.object(parse_api, 'GITHUB_TOKEN', 'fixture'), \
                    patch.object(source_files, 'get_source_doc', side_effect=AssertionError('unexpected source lookup')), \
                    patch.object(proofread_api, 'dispatch_proofread', AsyncMock()) as send_proof, \
                    patch.object(parse_api, 'dispatch_parse', AsyncMock()) as send_split:
                for endpoint, payload in (('/api/proofread', proof), ('/api/parse', split)):
                    response = self.client.post(endpoint, json=payload)
                    self.assertEqual(response.status_code, 503)
                    self.assertIn('保留草稿', response.json()['error'])
                send_proof.assert_not_awaited()
                send_split.assert_not_awaited()

    def test_explicit_resume_switch_only_restores_normal_validation(self):
        with patch.dict('os.environ', {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'}, clear=True), \
                patch.object(proofread_api, 'GITHUB_TOKEN', ''), patch.object(parse_api, 'GITHUB_TOKEN', ''):
            proof = self.client.post('/api/proofread', json={
                'doc_id': 'offline-proof', 'patch': {'version': 2, 'parts': {}, 'comments': {}}})
            split = self.client.post('/api/parse', json={
                'doc_id': 'offline-parse', 'articles': [{'title': '文章', 'page_start': 1,
                                                         'page_end': 1, 'content': '正文', 'base_part_count': 1}]})
        self.assertEqual(proof.status_code, 503)
        self.assertEqual(split.status_code, 503)
        self.assertEqual(proof.json()['error'], 'proofreading is not configured')
        self.assertEqual(split.json()['error'], 'parsing is not configured')

    def test_rejected_request_can_be_retried_after_explicit_resume(self):
        proofread_api.proofread_dispatches.clear()
        proofread_api.proofread_requests.clear()
        source = {'doc_id': 'doc-1', 'archive_id': 1, 'article_id': 'a', 'publication_id': 'p',
                  'article': {'parts': [{'text': '旧', 'type': 'paragraph'}], 'comments': []}}
        payload = {'doc_id': 'doc-1', 'base_content_hash': document_identity.content_hash(source),
                   'patch': {'version': 2, 'parts': {'0': {'diff': '-1\t+新'}}, 'comments': {}}}
        dispatch = AsyncMock()
        with patch.object(proofread_api, 'GITHUB_TOKEN', 'fixture'), \
                patch.object(source_files, 'get_source_doc', return_value=source), \
                patch.object(proofread_api, 'dispatch_proofread', dispatch):
            with patch.dict('os.environ', {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '1'}):
                stopped = self.client.post('/api/proofread', json=payload)
            self.assertEqual(stopped.status_code, 503)
            self.assertEqual(proofread_api.proofread_dispatches, {})
            self.assertEqual(proofread_api.proofread_requests, {})
            with patch.dict('os.environ', {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'}):
                resumed = self.client.post('/api/proofread', json=payload)
        self.assertEqual(resumed.status_code, 202)
        dispatch.assert_awaited_once()

    def test_missing_or_null_baseline_stops_both_routes_without_lookup_or_dispatch(self):
        proof = {"doc_id": "offline-proof", "patch": {"version": 2, "parts": {}, "comments": {}}}
        split = {"doc_id": "offline-parse", "articles": [{"title": "文章", "page_start": 1,
                 "page_end": 1, "content": "正文", "base_part_count": 1}]}
        with patch.dict('os.environ', {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'}), \
                patch.object(proofread_api, 'GITHUB_TOKEN', 'fixture'), patch.object(parse_api, 'GITHUB_TOKEN', 'fixture'), \
                patch.object(source_files, 'get_source_doc') as lookup, \
                patch.object(proofread_api, 'dispatch_proofread', AsyncMock()) as proof_send, \
                patch.object(parse_api, 'dispatch_parse', AsyncMock()) as parse_send:
            for endpoint, body in (("/api/proofread", proof), ("/api/parse", split)):
                for payload in (body, {**body, "base_content_hash": None}):
                    with self.subTest(endpoint=endpoint, payload=payload):
                        response = self.client.post(endpoint, json=payload)
                        self.assertEqual(response.status_code, 409)
                        self.assertIn("保留草稿", response.json()["error"])
                for invalid in ("bad", "G" * 64, 123):
                    with self.subTest(endpoint=endpoint, invalid=invalid):
                        response = self.client.post(endpoint, json={**body, "base_content_hash": invalid})
                        self.assertEqual(response.status_code, 422)
        lookup.assert_not_called()
        proof_send.assert_not_awaited()
        parse_send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
