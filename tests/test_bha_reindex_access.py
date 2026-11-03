import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import yaml

from scripts import check_bha_reindex_access as check


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.body[:limit]


class BhaReindexAccessTests(unittest.TestCase):
    def test_only_fixed_get_endpoint_is_called_and_output_is_sanitized(self):
        calls = []

        def request(req, timeout):
            calls.append((req, timeout))
            return Response(json.dumps({'index_status': 'ready', 'switch_pending': False,
                                        'archive_revisions': {'0': 'a', '1': 'b'},
                                        'job': {'private_detail': 'never-print'}}).encode())

        result = check.check('fixture-token', open_url=request)
        self.assertEqual(result, {'authenticated': True, 'index_status': 'ready',
                                  'switch_pending': False, 'archive_count': 2})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].full_url, check.URL)
        self.assertEqual(calls[0][0].get_method(), 'GET')
        self.assertEqual(calls[0][0].get_header('Authorization'), 'Bearer fixture-token')
        self.assertNotIn('fixture-token', json.dumps(result))
        self.assertNotIn('never-print', json.dumps(result))

    def test_missing_invalid_or_unready_response_fails_closed(self):
        def respond(value):
            return lambda *_args, **_kwargs: Response(value)

        with self.assertRaisesRegex(RuntimeError, 'not configured'):
            check.check('', open_url=lambda *_args, **_kwargs: self.fail('no request'))
        for value in (b'not json', b'{}', b'[]', b'x' * (check.MAX_BYTES + 1),
                      b'{"index_status":"starting","switch_pending":false,"archive_revisions":{}}',
                      b'{"index_status":"ready","switch_pending":true,"archive_revisions":{}}'):
            with self.subTest(value=value[:50]), self.assertRaises(RuntimeError):
                check.check('fixture-token', open_url=respond(value))

    def test_http_and_network_failures_never_print_secret_or_response_body(self):
        for failure in (HTTPError(check.URL, 401, 'fixture-token', {}, io.BytesIO(b'fixture-token')),
                        URLError('fixture-token')):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(check, 'urlopen', side_effect=failure):
                    with self.assertRaises(RuntimeError) as context:
                        check.check('fixture-token', open_url=check.urlopen)
                self.assertNotIn('fixture-token', str(context.exception))

    def test_workflow_is_manual_read_only_and_uses_only_the_expected_secret(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-reindex-auth-check.yml'
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        steps = workflow['jobs']['verify']['steps']
        self.assertTrue(any('PyYAML==6.0.3' in step.get('run', '') for step in steps))
        target = next(step for step in steps if step.get('name', '').startswith('Verify BHA status'))
        self.assertEqual(target['env'], {'BHA_REINDEX_TOKEN': '${{ secrets.BHA_REINDEX_TOKEN }}'})
        self.assertEqual(target['run'], 'python -B scripts/check_bha_reindex_access.py')
        self.assertFalse(any('gh workflow run' in step.get('run', '') for step in steps))


if __name__ == '__main__':
    unittest.main()
