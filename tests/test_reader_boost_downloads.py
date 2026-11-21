"""Canary workers must enforce planned source sizes before decoding or conversion."""
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import convert_reader_assets as converter
from scripts import pilot_image_page_stream as image, pilot_media_stream as media
from tests.test_reader_format_workflow_automation import workflow

URL = 'https://example.test/source'


class Response(io.BytesIO):
    def __init__(self, body, *, status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return super().read(size)


class BoostDownloadsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = Path(directory.name) / 'source'

    def test_mp3_fresh_download_exact_short_and_overflow(self):
        for body, error in ((b'abcd', None), (b'abc', 'shorter'), (b'abcde', 'exceeds')):
            with self.subTest(body=body):
                self.target.unlink(missing_ok=True)
                response = Response(body)
                with patch.object(converter.urllib.request, 'urlopen', return_value=response):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            converter.download_source(URL, self.target, exact_bytes=4)
                    else:
                        self.assertEqual(converter.download_source(URL, self.target, exact_bytes=4),
                                         (hashlib.sha256(body).hexdigest(), 4))
                        self.assertEqual(self.target.read_bytes(), body)
                        self.assertEqual(response.read_sizes, [5, 1])
                self.assertLessEqual(self.target.stat().st_size, 4)

    def test_mp3_resume_enforces_pin_with_cached_prefix(self):
        for suffix, content_range, error in (
                (b'cd', 'bytes 2-3/4', None), (b'c', 'bytes 2-3/4', 'shorter'),
                (b'cde', 'bytes 2-3/4', 'exceeds'), (b'cd', 'bytes 2-4/5', 'range differs')):
            with self.subTest(suffix=suffix, content_range=content_range):
                self.target.write_bytes(b'ab')
                response = Response(suffix, status=206, headers={'Content-Range': content_range})
                with patch.object(converter.urllib.request, 'urlopen', return_value=response) as open_url:
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            converter.download_source(URL, self.target, exact_bytes=4)
                    else:
                        self.assertEqual(converter.download_source(URL, self.target, exact_bytes=4),
                                         (hashlib.sha256(b'abcd').hexdigest(), 4))
                        self.assertEqual(self.target.read_bytes(), b'abcd')
                    self.assertEqual(open_url.call_args.args[0].headers['Range'], 'bytes=2-')
                if self.target.exists():
                    self.assertLessEqual(self.target.stat().st_size, 4)

    def test_mp3_retries_partial_transfer_and_does_not_accept_a_full_cache_without_eof(self):
        class Interrupted(Response):
            def read(self, size):
                if self.read_sizes:
                    raise TimeoutError('interrupted before EOF')
                return super().read(size)

        for prefix, next_response, expected_range in (
                (b'ab', Response(b'cd', status=206, headers={'Content-Range': 'bytes 2-3/4'}), 'bytes=2-'),
                (b'abcd', Response(b'abcd'), None)):
            with self.subTest(prefix=prefix):
                self.target.unlink(missing_ok=True)
                with patch.object(converter.urllib.request, 'urlopen', side_effect=[Interrupted(prefix), next_response]) as open_url, \
                        patch.object(converter.time, 'sleep'):
                    converter.download_source(URL, self.target, exact_bytes=4)
                self.assertEqual(self.target.read_bytes(), b'abcd')
                self.assertEqual(open_url.call_args.args[0].headers.get('Range'), expected_range)
        self.target.write_bytes(b'abcde')
        with patch.object(converter.urllib.request, 'urlopen') as open_url:
            with self.assertRaisesRegex(RuntimeError, 'cached source exceeds'):
                converter.download_source(URL, self.target, exact_bytes=4)
            open_url.assert_not_called()

    def test_jpg_stream_rejects_bad_lengths_before_decoding(self):
        class Stream(httpx.SyncByteStream):
            def __init__(self, chunks):
                self.chunks = chunks
            def __iter__(self):
                yield from self.chunks

        for chunks, status, error in (((b'ab', b'cd'), 200, None), ((b'abc',), 200, 'shorter'),
                                     ((b'abcd', b'e'), 200, 'exceeds'), ((b'abcd',), 206, 'complete')):
            with self.subTest(chunks=chunks, status=status):
                transport = httpx.MockTransport(lambda req: httpx.Response(status, stream=Stream(chunks)))
                with httpx.Client(transport=transport) as session, \
                        patch('huggingface_hub.utils.get_session', return_value=session), patch.object(image.time, 'sleep'):
                    if error:
                        with self.assertRaisesRegex(ValueError, error):
                            image.download(URL, self.target, None, exact_bytes=4)
                    else:
                        image.download(URL, self.target, None, exact_bytes=4)
                        self.assertEqual(self.target.read_bytes(), b'abcd')
                self.assertLessEqual(self.target.stat().st_size, 4)

    def test_workers_forward_size_only_in_canary_mode(self):
        class Stop(Exception):
            pass

        source = {'repo': 'VoiceOfML/Test', 'path': 'clip.mp3', 'revision': 'a' * 40,
                  'extension': 'mp3', 'source_bytes': 4}
        for canary in (False, True):
            with self.subTest(canary=canary), \
                    patch.object(converter, 'download_source', side_effect=Stop) as download:
                with self.assertRaises(Stop):
                    media.build_one(source, self.target.parent, media.TARGET_BUCKET, boost_canary=canary)
                self.assertEqual(download.call_args.kwargs, {'exact_bytes': 4} if canary else {})
            with self.subTest(canary=canary), patch.object(image, 'download', side_effect=Stop) as download:
                with self.assertRaises(Stop):
                    image.build_one({**source, 'extension': 'jpg', 'path': 'photo.jpg', 'bytes': 4},
                                    self.target.parent, None, image.TARGET_BUCKET, boost_canary=canary)
                self.assertEqual(download.call_args.kwargs, {'exact_bytes': 4} if canary else {})

    def test_workflow_workers_pass_explicit_canary_only_when_requested(self):
        for name, worker, ext in (('image-stream', 'build', 'jpg'), ('media-stream', 'media', 'mp3')):
            command = next(step['run'] for step in workflow(name)['jobs'][worker]['steps']
                           if '--apply --strict' in step.get('run', ''))
            for canary in (False, True):
                with self.subTest(name=name, canary=canary):
                    result = subprocess.run(['bash', '-eu', '-c', 'python() { printf "%s\\n" "$@"; }\n' + command],
                                            env={'BOOST_CANARY': str(canary).lower(), 'SHARD': '0',
                                                 'LIMIT': '4' if canary else '3', 'KIND': 'audio',
                                                 'EXTENSION': ext, 'RETRY_FAILURES': 'false'},
                                            check=True, capture_output=True, text=True)
                    self.assertEqual('--boost-canary' in result.stdout.splitlines(), canary)
