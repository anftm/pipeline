"""Pinned sample checks cannot turn partial BHA evidence into full acceptance."""
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from scripts import bha_consumer_sample as sample

COMMIT, TREE = 'a' * 40, 'b' * 40
PATH = 'mao/maoquanji27/002/002fff5d4e.json'
DOC = '0:10:002fff5d4e:maoquanji27'
ARTICLE = {'title': 'fixture', 'parts': [{'text': 'synthetic'}]}
TAGS = [{'name': 'fixture', 'type': 'subject'}]


def data(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def health(**changes):
    return data({'ok': True, 'index_ready': True, 'index_status': 'ready', 'es': True,
                 'document_count': 1, **changes})


def query(doc_id, _archive, _title):
    return data({'indexing': False, 'index_status': 'ready', 'total': 1, 'results': [{'doc_id': doc_id}]})


def fetch(url, _limit):
    if url.endswith('/api/health'):
        return health()
    if url.endswith('.tags'):
        return data(TAGS)
    if url.endswith('.json'):
        return data(ARTICLE)
    if '/api/preview/' in url:
        return data({'doc_id': DOC, 'archive_id': 0, 'article_id': '002fff5d4e',
                     'publication_id': 'maoquanji27', 'article': {**ARTICLE, 'tags': TAGS}})
    raise AssertionError(url)


class BhaConsumerSampleTests(unittest.TestCase):
    def test_exact_article_and_tags_checked_against_pinned_consumer(self):
        rows = [{'archive': 0, 'path': PATH}]
        calls = []
        def lookup(token, number):
            calls.append((token, number)); return '0', COMMIT, TREE
        result = sample.observe('token', rows, fetch=fetch, lookup=lookup, query=query)
        self.assertEqual(calls, [('token', 0), ('token', 0)])
        self.assertEqual(result['samples'][0]['doc_id'], DOC)
        self.assertEqual(result['samples'][0]['source_sha256'], hashlib.sha256(data(ARTICLE)).hexdigest())
        self.assertEqual(result['samples'][0]['tags_sha256'], hashlib.sha256(data(TAGS)).hexdigest())
        self.assertTrue(result['samples'][0]['preview_article_equal'])
        self.assertTrue(result['samples'][0]['exact_title_search_found'])
        self.assertEqual(result['remote_writes'], 0)

    def test_sidecar_replaces_embedded_tags_including_empty_lists(self):
        embedded = [{'name': 'embedded', 'type': 'subject'}]
        for embedded_tags, sidecar in ((TAGS, TAGS), (embedded, TAGS), (embedded, []),
                                       ([], TAGS), ([], [])):
            with self.subTest(embedded=embedded_tags, sidecar=sidecar):
                article = {**ARTICLE, 'tags': embedded_tags, 'description': 'preserve me',
                           'extra': {'nested': ['also preserve me']}}
                def tagged_fetch(url, limit):
                    if url.endswith('.json'):
                        return data(article)
                    if url.endswith('.tags'):
                        return data(sidecar)
                    preview = json.loads(fetch(url, limit))
                    preview['article'] = {**article, 'tags': sidecar}
                    return data(preview)
                result = sample.observe_one(0, COMMIT, PATH, fetch=tagged_fetch, query=query)
                self.assertTrue(result['preview_article_equal'])
                self.assertTrue(result['exact_title_search_found'])
                self.assertEqual(result['source_sha256'], hashlib.sha256(data(article)).hexdigest())
                self.assertEqual(result['tags_sha256'], hashlib.sha256(data(sidecar)).hexdigest())

    def test_embedded_tags_do_not_hide_preview_mismatches(self):
        embedded = [{'name': 'embedded', 'type': 'subject'}]
        article = {**ARTICLE, 'tags': embedded, 'extra': {'nested': 'preserve me'}}
        for sidecar in (TAGS, []):
            expected = {**article, 'tags': sidecar}
            wrong_articles = (
                article,  # Embedded tags retained instead of the sidecar.
                {**expected, 'tags': embedded + sidecar},  # Tags concatenated.
                {key: value for key, value in expected.items() if key != 'tags'},
                {key: value for key, value in expected.items() if key != 'extra'},
                {**expected, 'extra': {'nested': 'changed'}},
                {**expected, 'parts': [{'text': 'changed'}]},
                {**expected, 'unexpected': 'field'},
            )
            for wrong_article in wrong_articles:
                with self.subTest(sidecar=sidecar, preview=wrong_article):
                    def wrong(url, limit):
                        if url.endswith('.json'):
                            return data(article)
                        if url.endswith('.tags'):
                            return data(sidecar)
                        preview = json.loads(fetch(url, limit))
                        preview['article'] = wrong_article
                        return data(preview)
                    search = Mock(side_effect=AssertionError('search after a mismatched preview'))
                    with self.assertRaisesRegex(ValueError, 'preview differs'):
                        sample.observe_one(0, COMMIT, PATH, fetch=wrong, query=search)
                    search.assert_not_called()

    def test_embedded_tags_do_not_replace_missing_or_invalid_sidecar(self):
        for sidecar in (None, {}, 'bad', b'bad', FileNotFoundError('missing sidecar')):
            with self.subTest(sidecar=sidecar):
                def invalid(url, limit):
                    if url.endswith('.json'):
                        return data({**ARTICLE, 'tags': TAGS})
                    if url.endswith('.tags'):
                        if isinstance(sidecar, Exception):
                            raise sidecar
                        return sidecar if isinstance(sidecar, bytes) else data(sidecar)
                    raise AssertionError('preview requested with invalid sidecar')
                error = FileNotFoundError if isinstance(sidecar, Exception) else ValueError
                with self.assertRaises(error):
                    sample.observe_one(0, COMMIT, PATH, fetch=invalid, query=query)

    def test_invalid_paths_cannot_escape_or_cross_archive(self):
        bad = ('../mao/002/002fff5d4e.json', 'mao/../002/002fff5d4e.json',
               'mao/pub/002/002fff5d4e.tags', 'mao/pub/003/002fff5d4e.json',
               'mao/pub/002/../../other.json', 'mao\\pub/002/002fff5d4e.json')
        for path in bad:
            with self.subTest(path=path), self.assertRaises(ValueError):
                sample.sample_path(0, COMMIT, path)
        for archive in (-1, 32, True):
            with self.subTest(archive=archive), self.assertRaises(ValueError):
                sample.sample_path(archive, COMMIT, PATH)
        with self.assertRaises(ValueError): sample.sample_path(0, 'main', PATH)

    def test_preview_mismatch_or_missing_tags_is_not_accepted(self):
        def wrong(url, limit):
            if '/api/preview/' in url:
                return data({'doc_id': DOC, 'archive_id': 0, 'article_id': '002fff5d4e',
                             'publication_id': 'maoquanji27', 'article': {**ARTICLE, 'tags': []}})
            return fetch(url, limit)
        with self.assertRaisesRegex(ValueError, 'preview differs'):
            sample.observe_one(0, COMMIT, PATH, fetch=wrong, query=query)
        with self.assertRaisesRegex(ValueError, 'invalid BHA tags JSON'):
            sample.observe_one(0, COMMIT, PATH, fetch=lambda url, limit: b'bad' if url.endswith('.tags') else fetch(url, limit),
                               query=query)

    def test_moving_head_or_unready_consumer_stops(self):
        states = iter([('0', COMMIT, TREE), ('0', 'c' * 40, TREE)])
        with self.assertRaisesRegex(RuntimeError, 'revision changed'):
            sample.observe('token', [{'archive': 0, 'path': PATH}], fetch=fetch, lookup=lambda *_: next(states), query=query)
        with self.assertRaisesRegex(ValueError, 'not ready'):
            sample.observe('token', [{'archive': 0, 'path': PATH}],
                           fetch=lambda *_: health(index_ready=False), lookup=lambda *_: ('0', COMMIT, TREE))
        values = iter([health(), data(ARTICLE), data(TAGS), fetch(BASE_PREVIEW, 10), health(document_count=2)])
        with self.assertRaisesRegex(RuntimeError, 'health changed'):
            sample.observe('token', [{'archive': 0, 'path': PATH}],
                           fetch=lambda *_: next(values), lookup=lambda *_: ('0', COMMIT, TREE), query=query)

    def test_missing_or_incomplete_search_result_is_not_a_success(self):
        for result in ({'indexing': True, 'index_status': 'ready', 'total': 1, 'results': [{'doc_id': DOC}]},
                       {'indexing': False, 'index_status': 'ready', 'total': 1, 'results': []},
                       {'indexing': False, 'index_status': 'ready', 'total': 101, 'results': [{'doc_id': DOC}]}):
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, 'search did not return'):
                sample.observe_one(0, COMMIT, PATH, fetch=fetch, query=lambda *_, result=result: data(result))

    def test_bounded_get_never_accepts_large_body(self):
        response = Mock(); response.status = 200; response.read.return_value = b'1234'
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *_args: None
        with patch.object(sample.urllib.request, 'urlopen', return_value=response), \
                self.assertRaisesRegex(ValueError, 'exceeds'):
            sample.get('https://example.invalid', 3)


BASE_PREVIEW = sample.BASE + '/api/preview/' + DOC


if __name__ == '__main__':
    unittest.main()
