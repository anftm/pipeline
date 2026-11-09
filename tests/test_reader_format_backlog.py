"""Backlog threshold must reflect verified source content, not revision churn."""
import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import reader_format_backlog as subject
from scripts import reader_assets, reader_stream_index, search_publication_pointer, search_snapshot

OLD, NEW, SHA_A, SHA_B = 'a' * 40, 'b' * 40, '1' * 64, '2' * 64
REPO = 'VoiceOfML/Test'


def corpus(names):
    return {'v': 2, 'rp': [REPO], 'fd': [[]],
            'rc': [[0, name, ext, 0, 1, 0] for name, ext in names]}


def item(name, ext='epub'):
    path = name + '.' + ext
    return {'key': REPO + '\0' + path, 'repo': REPO, 'path': path,
            'extension': ext, 'revision': NEW, 'bytes': 1}


def row(name, revision=OLD, digest=SHA_A):
    obj = item(name)
    return {'key': obj['key'], 'source_revision': revision, 'source_sha256': digest,
            'source_extension': 'epub', 'bucket': subject.READER_BUCKET,
            'manifest': 'chapters/ebook/epub/' + name + '/chapter-manifest.json'}


def route(entry):
    return {'s': 2, 'm': 'e', 'b': subject.READER_BUCKET, 'p': entry['manifest']}


class Metadata:
    def __init__(self, values): self.values, self.calls = values, []
    def get_paths_info(self, repo, paths, *, repo_type, revision, expand):
        self.calls.append((repo, revision, tuple(paths)))
        return [self.values[(revision, path)] for path in paths if (revision, path) in self.values]


def file(path, *, lfs=None, blob=None):
    return SimpleNamespace(path=path, lfs={'sha256': lfs} if lfs is not None else None, blob_id=blob)


class ReaderFormatBacklogTests(unittest.TestCase):
    def test_epub_same_revision_lfs_blob_changed_unknown_and_new(self):
        names = ('same', 'lfs-same', 'lfs-changed', 'git-same', 'git-changed', 'unknown', 'mismatch', 'new')
        records = {item(name)['key']: item(name) for name in names}
        entries = [row(name, NEW if name == 'same' else OLD) for name in names if name != 'new']
        index = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': entries, 'failures': []}
        routes = {entry['key']: route(entry) for entry in entries}
        routes[item('mismatch')['key']] = {'s': 2, 'm': 'e', 'p': 'other', 'b': subject.READER_BUCKET}
        values = {
            (NEW, 'lfs-same.epub'): file('lfs-same.epub', lfs=SHA_A, blob='d' * 40),
            (NEW, 'lfs-changed.epub'): file('lfs-changed.epub', lfs=SHA_B, blob='e' * 40),
            (NEW, 'git-same.epub'): file('git-same.epub', blob='1' * 40),
            (OLD, 'git-same.epub'): file('git-same.epub', blob='1' * 40),
            (NEW, 'git-changed.epub'): file('git-changed.epub', blob='2' * 40),
            (OLD, 'git-changed.epub'): file('git-changed.epub', blob='3' * 40),
            (NEW, 'unknown.epub'): file('unknown.epub', blob='4' * 40),
        }
        api = Metadata(values)
        result = subject.classify_epub(records, routes, index, [], api)
        expected = {'same': 'same_revision', 'lfs-same': 'content_unchanged',
                    'lfs-changed': 'content_changed', 'git-same': 'content_unchanged',
                    'git-changed': 'content_changed', 'unknown': 'source_identity_unknown',
                    'mismatch': 'route_or_index_mismatch', 'new': 'not_mapped'}
        self.assertEqual({name: result[item(name)['key']]['state'] for name in names}, expected)
        self.assertEqual(result[item('lfs-changed')['key']]['current_source_sha256'], SHA_B)
        self.assertEqual(result[item('git-changed')['key']]['proof'], 'exact-revision-git-blobs')
        self.assertEqual(result[item('git-changed')['key']]['previous_git_blob'], '3' * 40)
        self.assertFalse(any(path == 'same.epub' for _repo, _rev, paths in api.calls for path in paths))

    def test_switching_lfs_to_git_does_not_infer_content_change(self):
        records = {item('book')['key']: item('book')}; entry = row('book')
        index = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [entry], 'failures': []}
        metadata = {(NEW, 'book.epub'): file('book.epub', blob='b' * 40),
                    (OLD, 'book.epub'): file('book.epub', lfs=SHA_A, blob='a' * 40)}
        result = subject.classify_epub(records, {entry['key']: route(entry)}, index, [], Metadata(metadata))
        self.assertEqual(result[entry['key']]['state'], 'source_identity_unknown')

    def test_web_content_comparison_is_not_a_claim_about_relative_resources(self):
        source = item('page', 'html');key = source['key']
        entry = {'key': key, 'source_revision': OLD, 'source_sha256': SHA_A, 'bucket': subject.READER_BUCKET,
                 'object': 'documents/web/html/hash/document.html',
                 'manifest': 'documents/web/html/hash/document-manifest.json'}
        route = {'s': 2, 'm': 'h', 'b': subject.READER_BUCKET, 'p': entry['object']}
        canonical = {'version': 1, 'kind': 'web-document-stream-index', 'files': [entry], 'failures': []}
        api = Metadata({(NEW, 'page.html'): file('page.html', lfs=SHA_B)})
        rows = subject.classify_indexed({key: source}, {key: route}, canonical, [], api,
                                        extensions={'html'}, kind='web-document-stream-index', route_modes={'h'})
        self.assertEqual(rows[key]['state'], 'content_changed')
        report = {row['extension']: row for row in subject.summarize(
            {key: source}, {key: route}, rows, threshold=0, checked_extensions={'html'})}['html']
        self.assertEqual(report['source_content_changed'], 1)
        self.assertTrue(report['threshold_exceeded'])
        self.assertFalse(report['trigger_ready'])

    def test_image_content_uses_image_path_and_preserves_canonical_merge_rules(self):
        source = item('scan', 'jpg');key = source['key']
        entry = {'key': key, 'source_revision': OLD, 'source_sha256': SHA_A, 'bucket': subject.READER_BUCKET,
                 'mode': 'image', 'path': 'pages/image/jpg/hash/document.webp'}
        route = {'s': 2, 'm': 'i', 'b': subject.READER_BUCKET, 'p': entry['path']}
        canonical = {'version': 1, 'kind': 'image-stream-index', 'files': [entry]}
        api = Metadata({(NEW, 'scan.jpg'): file('scan.jpg', lfs=SHA_B)})
        def image_merge(before, parts, _kind, _extensions, count):
            return subject.finalize_image_index.merge(before, parts, count)
        rows = subject.classify_indexed({key: source}, {key: route}, canonical, [], api,
                                        extensions={'jpg'}, kind='image-stream-index', route_modes={'i', 'f'},
                                        merge=image_merge)
        self.assertEqual(rows[key]['state'], 'content_changed')
        report = {row['extension']: row for row in subject.summarize(
            {key: source}, {key: route}, rows, threshold=0, checked_extensions={'jpg'})}['jpg']
        self.assertEqual(report['source_content_changed'], 1)
        self.assertTrue(report['all_source_identities_checked'])
        foreign = {'version': 1, 'kind': 'image-stream-index', 'files': [{**entry, 'key': 'other'}]}
        bad_shard = (reader_stream_index.shard_for('other', 8) + 1) % 8
        with self.assertRaisesRegex(ValueError, 'duplicate or foreign'):
            subject.classify_indexed({key: source}, {key: route}, canonical, [(bad_shard, foreign)], api,
                                     extensions={'jpg'}, kind='image-stream-index', route_modes={'i', 'f'},
                                     merge=image_merge)

    def test_media_changed_bytes_are_counted_even_when_route_exists(self):
        source = item('audio', 'mp3');key = source['key']
        entry = {'key': key, 'source_revision': OLD, 'source_sha256': SHA_A, 'bucket': subject.READER_BUCKET,
                 'object': 'media/audio/mp3/hash/audio.mp3', 'profile': 'native-media-cdn-v1'}
        route = {'s': 2, 'm': 'a', 'b': subject.READER_BUCKET, 'p': entry['object']}
        canonical = {'version': 1, 'kind': 'media-stream-index', 'files': [entry], 'failures': []}
        rows = subject.classify_indexed({key: source}, {key: route}, canonical, [],
                                       Metadata({(NEW, 'audio.mp3'): file('audio.mp3', lfs=SHA_B)}),
                                       extensions={'mp3'}, kind='media-stream-index', route_modes={'a', 'v', 'f'})
        self.assertEqual(rows[key]['state'], 'content_changed')
        report = {r['extension']: r for r in subject.summarize(
            {key: source}, {key: route}, rows, threshold=0, checked_extensions={'mp3'})}['mp3']
        self.assertEqual(report['source_content_changed'], 1)
        self.assertTrue(report['all_source_identities_checked'])
        self.assertFalse(report['trigger_ready'])

    def test_office_and_non_epub_books_compare_content_against_their_own_route(self):
        for extension in ('docx', 'mobi', 'chm'):
            with self.subTest(extension=extension):
                source = item('updated', extension);key = source['key']
                entry = {'key': key, 'source_revision': OLD, 'source_sha256': SHA_A,
                         'source_extension': extension, 'bucket': subject.READER_BUCKET,
                         'manifest': 'chapters/ebook/' + extension + '/hash/chapter-manifest.json'}
                route = {'s': 2, 'm': 'e', 'b': subject.READER_BUCKET, 'p': entry['manifest']}
                canonical = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [entry], 'failures': []}
                rows = subject.classify_indexed({key: source}, {key: route}, canonical, [],
                    Metadata({(NEW, 'updated.' + extension): file('updated.' + extension, lfs=SHA_B)}),
                    extensions={extension}, kind='ebook-chapter-stream-index', route_modes={'e'})
                self.assertEqual(rows[key]['state'], 'content_changed')
                result = {r['extension']: r for r in subject.summarize(
                    {key: source}, {key: route}, rows, threshold=0, checked_extensions={extension})}[extension]
                self.assertTrue(result['threshold_exceeded'])
                self.assertEqual(result['source_content_changed'], 1)
                self.assertFalse(result['trigger_ready'])

    def test_malformed_lfs_or_wrong_response_never_becomes_content_proof(self):
        records = {item('book')['key']: item('book')}; entry = row('book')
        index = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [entry], 'failures': []}
        malformed = {(NEW, 'book.epub'): file('book.epub', lfs='bad', blob='a' * 40)}
        with self.assertRaisesRegex(ValueError, 'malformed source LFS'):
            subject.classify_epub(records, {entry['key']: route(entry)}, index, [], Metadata(malformed))
        api = Mock(); api.get_paths_info.return_value = [file('foreign.epub', lfs=SHA_A)]
        with self.assertRaisesRegex(ValueError, 'foreign'):
            subject.classify_epub(records, {entry['key']: route(entry)}, index, [], api)

    def test_shard_change_and_current_failure_are_visible_but_never_published(self):
        source = item('book'); entry = row('book', NEW)
        number = reader_stream_index.shard_for(source['key'], 8)
        later = {**entry, 'checkpointed_at': '2026-01-01T00:00:00Z', 'manifest': 'other'}
        index = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [entry], 'failures': []}
        shard = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [later],
                 'failures': [{'key': source['key'], 'source_revision': NEW}]}
        result = subject.classify_epub({source['key']: source}, {source['key']: route(entry)}, index,
                                       [(number, shard)], Metadata({}))
        self.assertTrue(result[source['key']]['pending_shard_checkpoint'])
        self.assertTrue(result[source['key']]['recorded_failure'])
        self.assertEqual(index['files'], [entry])
        with self.assertRaisesRegex(ValueError, 'duplicate Reader shard'):
            subject.classify_epub({source['key']: source}, {source['key']: route(entry)}, index,
                                  [(number, shard), (number, shard)], Metadata({}))

    def test_threshold_strictly_above_100_and_old_mapped_routes_not_claimed_current(self):
        records = {item(str(n))['key']: item(str(n)) for n in range(101)}
        rows = {key: {'state': 'not_mapped'} for key in records}
        other = item('image', 'jpg'); records[other['key']] = other
        rows[item('0')['key']] = {'state': 'content_changed'}
        report = {r['extension']: r for r in subject.summarize(records, {other['key']: {'s': 2}}, rows)}
        self.assertEqual(report['epub']['definite_backlog'], 101)
        self.assertTrue(report['epub']['threshold_exceeded'])
        self.assertTrue(report['epub']['all_source_identities_checked'])
        self.assertFalse(report['epub']['trigger_ready'])
        self.assertEqual(report['jpg']['mapped_content_not_checked'], 1)
        self.assertFalse(report['jpg']['all_source_identities_checked'])
        del records[item('100')['key']]; del rows[item('100')['key']]
        r = {x['extension']: x for x in subject.summarize(records, {}, rows)}['epub']
        self.assertEqual(r['definite_backlog'], 100)
        self.assertFalse(r['threshold_exceeded'])

    def test_source_payload_rejects_missing_revision_and_duplicate_key(self):
        source = corpus([('a', 'epub')])
        with self.assertRaises(ValueError): subject.source_records(source, {})
        duplicate = corpus([('a', 'epub'), ('a', 'epub')])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            subject.source_records(duplicate, {REPO: NEW})

    def test_capture_checks_corpus_and_pointer_identity_without_mutations(self):
        raw_corpus = json.dumps(corpus([('a', 'epub')]), separators=(',', ':')).encode()
        compressed = gzip.compress(raw_corpus, mtime=0)
        digest = hashlib.sha256(compressed).hexdigest()
        member = {'sha256': digest, 'bytes': len(compressed), 'object': f'objects/sha256/{digest[:2]}/{digest}'}
        manifest = {'version': 1, 'kind': 'search-snapshot-local', 'record_count': 1,
                    'source_revisions': {REPO: NEW}, 'files': {'pages': {'search_data.json.gz':
                        {'sha256': digest, 'bytes': len(compressed)}}}}
        generation = hashlib.sha256(search_snapshot.canonical(manifest)).hexdigest()
        manifest.update(generation=generation, object_layout='objects/sha256/<first-two>/<sha256>',
                        objects={'pages': {'search_data.json.gz': member}}, remote_writes='none', publication_ready=False)
        pointer = search_publication_pointer.make_pointer(generation, None, '1' * 32)
        class Store:
            def __init__(self): self.reads = 0
            def read_bytes(self, bucket, path):
                if path == subject.CURRENT:
                    self.reads += 1; return pointer
                if path.endswith('/manifest.json'): return search_snapshot.canonical(manifest)
                return compressed
        store = Store()
        got = subject.capture_search(store, observe=lambda *_a, **_k: {'both': True})
        self.assertEqual(got[2], corpus([('a', 'epub')]))
        self.assertEqual(store.reads, 2)
        with patch.object(subject.search_generation, '_validate_corpus', side_effect=ValueError('bad corpus')):
            with self.assertRaisesRegex(ValueError, 'bad corpus'):
                subject.capture_search(store, observe=lambda *_a, **_k: {})

    def test_monitor_is_read_only_and_never_dispatches_or_deletes(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / '.github/workflows/reader-backlog-monitor.yml'
        flow = yaml.load(path.read_text(encoding='utf8'), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'schedule', 'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['inspect']['steps'])
        self.assertIn('scripts/reader_format_backlog.py --threshold 100', commands)
        for forbidden in ('--apply', 'workflow run', 'git push', 'delete', 'OCR'):
            self.assertNotIn(forbidden, commands)
        self.assertEqual(flow['concurrency']['queue'], 'max')

    def test_unknown_source_blocks_identity_completeness(self):
        source = item('book'); records = {source['key']: source}
        report = {r['extension']: r for r in subject.summarize(records, {},
                 {source['key']: {'state': 'source_identity_unknown'}})}['epub']
        self.assertFalse(report['all_source_identities_checked'])
        self.assertEqual(report['source_identity_unknown'], 1)
        self.assertFalse(report['trigger_ready'])


if __name__ == '__main__': unittest.main()
