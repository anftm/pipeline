"""Read-only CCRD TXT checker contracts; all responses are synthetic."""
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from scripts import check_ccrd_txt_only as checker


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


class CcrdTxtOnlyTests(unittest.TestCase):
    def setUp(self):
        self.revision = 'a' * 40
        self.txt = '合成目标正文'.encode()
        self.generation = 'ccrd-four-' + 'b' * 64
        self.manifest = {'format': 2, 'kind': 'ccrd-four-source-generation', 'generation': self.generation,
                         'base_metadata_sha256': 'c' * 64,
                         'databases': {name: {} for name in ('CCRD', 'CW', 'RMRB', 'PLA')},
                         'source_overrides': {checker.TARGET: {'sha256': hashlib.sha256(self.txt).hexdigest()}}}
        self.manifest_raw = encode(self.manifest)
        self.activation = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': checker.BUCKET,
                           'path': f'generations/{self.generation}/manifest.json',
                           'sha256': hashlib.sha256(self.manifest_raw).hexdigest()}
        self.calls = []
        self.upstream = self.txt
        self.site = self.txt
        self.api_reads = 0
        self.status_reads = 0
        self.status = {'ready': True, 'bucket': {'state': 'ready', 'source': 'space-activation',
                       'generation': self.generation, 'base_metadata_sha256': 'c' * 64,
                       'sources': ['CCRD', 'CW', 'RMRB', 'PLA']}}

    def read(self, url, *, limit):
        self.calls.append((url, limit))
        if url == f'https://huggingface.co/api/spaces/{checker.SPACE}':
            self.api_reads += 1
            return encode({'sha': self.revision, 'runtime': {'sha': self.revision, 'stage': 'RUNNING'}})
        if url.endswith('/data/index-release.json'):
            return encode(self.activation)
        if url == 'https://vomebook-search.hf.space/api/index-status':
            self.status_reads += 1
            return encode(self.status)
        if '/buckets/' in url:
            return self.manifest_raw
        if url.startswith('https://raw.githubusercontent.com/'):
            return self.upstream
        if url.startswith('https://huggingface.co/spaces/'):
            return self.site
        raise AssertionError('unexpected read-only endpoint')

    def test_matching_text_has_no_write_or_publish_authority(self):
        result = checker.check(read=self.read)
        self.assertTrue(result['upstream_matches_site'])
        self.assertEqual(result['space_revision'], self.revision)
        self.assertEqual(result['index_manifest_sha256'], self.activation['sha256'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertFalse(result['publication_ready'])
        self.assertFalse(result['external_writers_fenced'])
        self.assertTrue(result['consumer_status_matches_generation'])
        self.assertFalse(result['complete_index_contents_verified'])
        self.assertEqual(self.api_reads, 2)
        self.assertEqual(self.status_reads, 2)
        self.assertTrue(all(limit in (checker.MAX_TXT_BYTES, checker.MAX_METADATA_BYTES) for _url, limit in self.calls))

    def test_changed_upstream_is_reported_not_published(self):
        self.upstream = b'changed synthetic source'
        result = checker.check(read=self.read)
        self.assertFalse(result['upstream_matches_site'])
        self.assertEqual(result['remote_writes'], 0)
        with patch.object(checker, 'check', return_value=result), patch('sys.argv', ['check']):
            self.assertEqual(checker.main(), 2)

    def test_rejects_stale_activation_wrong_site_and_changed_space(self):
        for mode in ('manifest-hash', 'site', 'space-race', 'legacy', 'unknown-source'):
            self.setUp()
            if mode == 'manifest-hash':
                self.activation['sha256'] = '0' * 64
            elif mode == 'site':
                self.site = b'different site text'
            elif mode == 'legacy':
                self.manifest['format'] = 1
                self.manifest_raw = encode(self.manifest)
                self.activation['sha256'] = hashlib.sha256(self.manifest_raw).hexdigest()
            elif mode == 'unknown-source':
                self.manifest['databases']['OTHER'] = {}
                self.manifest_raw = encode(self.manifest)
                self.activation['sha256'] = hashlib.sha256(self.manifest_raw).hexdigest()
            original = self.read

            def read(url, *, limit):
                raw = original(url, limit=limit)
                if mode == 'space-race' and url.endswith('/api/spaces/vomebook/Search') and self.api_reads == 2:
                    return encode({'sha': 'c' * 40, 'runtime': {'sha': 'c' * 40, 'stage': 'RUNNING'}})
                return raw

            with self.subTest(mode=mode), self.assertRaises(ValueError):
                checker.check(read=read)

    def test_upstream_only_falls_back_on_404(self):
        calls = []

        def read(url, *, limit):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError('read-only GET failed: HTTP 404')
            return self.txt

        self.assertEqual(checker.upstream(read), self.txt)
        self.assertEqual(len(calls), 2)
        calls.clear()
        selected, raw = checker.upstream_snapshot(read)
        self.assertEqual(raw, self.txt)
        self.assertEqual(selected, calls[-1])
        self.assertEqual(len(calls), 2)
        for message in ('read-only GET failed: HTTP 403', 'read-only GET failed: network error'):
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                checker.upstream(lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(message)))

    def test_observation_retains_one_exact_upstream_response_without_refetching(self):
        self.upstream = b'first upstream revision'
        original = self.read
        requests = []

        def changing_upstream(url, *, limit):
            if url.startswith('https://raw.githubusercontent.com/'):
                requests.append(url)
                if len(requests) > 1:
                    return b'later upstream revision'
            return original(url, limit=limit)

        result = checker.observe(read=changing_upstream)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['upstream_txt'], b'first upstream revision')
        self.assertEqual(result['upstream_url'], requests[0])
        self.assertEqual(result['site_txt'], self.site)
        self.assertEqual(result['activation_bytes'], encode(self.activation))
        self.assertEqual(result['manifest_bytes'], self.manifest_raw)
        self.assertEqual(result['report']['upstream_txt_sha256'], hashlib.sha256(result['upstream_txt']).hexdigest())
        self.assertFalse(result['report']['upstream_matches_site'])
        self.assertFalse(result['report']['publication_ready'])

    def test_inactive_or_lagging_runtime_is_not_treated_as_deployed(self):
        for runtime in ({'stage': 'BUILDING', 'sha': self.revision},
                        {'stage': 'RUNNING', 'sha': 'd' * 40}, None):
            with self.subTest(runtime=runtime), self.assertRaisesRegex(ValueError, 'running revision'):
                checker._head({'sha': self.revision, 'runtime': runtime})

    def test_index_status_must_match_pinned_generation_and_sources(self):
        for key, value in [('ready', False), ('bucket', None)]:
            self.setUp()
            self.status[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'has not loaded'):
                checker.check(read=self.read)
        for key, value in [('generation', 'other'), ('base_metadata_sha256', 'd' * 64),
                           ('sources', ['CCRD', 'CW']), ('sources', ['CCRD', 'CW', 'RMRB', {}]),
                           ('sources', ['CCRD', 'CW', 'RMRB', 'PLA', 'PLA']), ('state', 'loading')]:
            self.setUp()
            self.status['bucket'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'has not loaded'):
                checker.check(read=self.read)

    def test_consumer_advancing_during_check_is_not_reported_as_matching(self):
        read_fixture = self.read

        def read(url, *, limit):
            raw = read_fixture(url, limit=limit)
            if url.endswith('/api/index-status') and self.status_reads == 2:
                changed = json.loads(raw)
                changed['bucket']['generation'] = 'ccrd-four-' + 'd' * 64
                return encode(changed)
            return raw

        with self.assertRaisesRegex(ValueError, 'has not loaded'):
            checker.check(read=read)

    def test_redirects_never_forward_authorization_cross_origin(self):
        handler = checker.ReadOnlyRedirects()
        request = Request('https://huggingface.co/example', headers={'Authorization': 'Bearer fixture'})
        redirected = handler.redirect_request(request, None, 302, '', {}, 'https://us.aws.cdn.hf.co/file')
        self.assertFalse(redirected.has_header('Authorization'))
        with self.assertRaises(RuntimeError):
            handler.redirect_request(request, None, 302, '', {}, 'https://untrusted.example/file')

    def test_response_size_and_http_errors_fail_closed(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _limit):
                return b'12345'

        with patch.object(checker, 'build_opener') as opener:
            opener.return_value.open.return_value = Response()
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                checker.read_url('https://huggingface.co/fixture', limit=4)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertFalse(request.has_header('Authorization'))
        error = HTTPError('https://huggingface.co/fixture', 403, 'private', {}, io.BytesIO(b'private'))
        with patch.object(checker, 'build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                checker.read_url('https://huggingface.co/fixture', limit=4)
        with self.assertRaises(ValueError):
            checker.read_url('https://untrusted.example/file', limit=4)

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        for raw in (b'{"sha":1,"sha":2}', b'{"status":NaN}', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                checker.json_object(raw, 'fixture')

    def test_workflow_is_manual_no_secrets_and_contains_only_read_checks(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / '.github/workflows/ccrd-txt-readonly-check.yml'
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(set(workflow['jobs']), {'check'})
        job = workflow['jobs']['check']
        self.assertEqual(job['if'], "github.repository == 'anftm/pipeline' && github.ref == 'refs/heads/main'")
        self.assertEqual(job['timeout-minutes'], '10')
        self.assertEqual([step.get('uses') for step in job['steps'][:2]],
                         ['actions/checkout@v7', 'actions/setup-python@v7'])
        self.assertEqual([step.get('run') for step in job['steps'][2:]],
                         ['python -m pip install PyYAML==6.0.3',
                          'python -B -m unittest tests.test_check_ccrd_txt_only -v',
                          'python -B scripts/check_ccrd_txt_only.py'])
        self.assertNotIn('env', job)
        self.assertFalse(any('env' in step or 'if' in step or 'continue-on-error' in step for step in job['steps']))
        self.assertNotIn('secrets.', path.read_text())


if __name__ == '__main__':
    unittest.main()
