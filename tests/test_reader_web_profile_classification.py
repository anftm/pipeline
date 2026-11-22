"""Opt-in web profile migrations need the current route and exact source identity."""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import reader_format_backlog as backlog
from scripts import reader_format_sources as sources
from scripts.reader_web_profile import WEB_PROFILE
from scripts.search_publication_pointer import make_pointer

REPO = 'VoiceOfML/Books'
OLD, NEW, BLOB = 'a' * 40, 'b' * 40, 'c' * 40
SOURCE, CHANGED, DOCUMENT = '1' * 64, '2' * 64, '3' * 64
SEARCH, READER = '4' * 64, '5' * 64
WEB_INDEX = 'web-document-stream-index'


def web_row(name, extension='html', revision=NEW, **fields):
    path = f'{name}.{extension}'
    key = f'{REPO}\0{path}'
    root = f'documents/web/{extension}/' + 'd' * 64
    source = {'key': key, 'repo': REPO, 'path': path, 'extension': extension, 'revision': NEW, 'bytes': 1}
    entry = {**{k: source[k] for k in ('key', 'repo', 'path', 'extension')},
             'source_revision': revision, 'source_sha256': SOURCE, 'sha256': DOCUMENT, 'bytes': 123,
             'mode': 'document-stream', 'bucket': backlog.READER_BUCKET, 'object': root + '/document.html',
             'manifest': root + '/document-manifest.json', **fields}
    route = {'s': 2, 'm': 'h', 'b': backlog.READER_BUCKET, 'p': root + '/document.html'}
    return source, entry, route


def index(*entries, kind=WEB_INDEX, failures=()):
    return {'version': 1, 'kind': kind, 'files': list(entries), 'failures': list(failures)}


def metadata(path, digest=None, blob=None):
    return {'path': path, 'lfs': {'sha256': digest} if digest else None, 'blob_id': blob}


class WebProfileClassificationTests(unittest.TestCase):
    def classify(self, source, entry, route, *, values=None, failures=(), enabled=True, kind=WEB_INDEX):
        key = source['key']
        api = Mock()
        api.get_paths_info.side_effect = lambda _repo, paths, *, revision, **_kw: [
            (values or {})[revision, path] for path in paths if (revision, path) in (values or {})]
        return backlog.classify_indexed({key: source}, {} if route is None else {key: route},
                                        index(*([] if entry is None else [entry]), kind=kind, failures=failures),
                                        [], api, extensions={source['extension']}, kind=kind, route_modes={'h'},
                                        include_profile_upgrades=False, include_web_profile_upgrades=enabled)[key]

    def test_only_reviewed_old_web_profiles_and_independent_opt_in(self):
        for extension in ('html', 'htm', 'mht', 'mhtml'):
            source, entry, route = web_row('book', extension)
            with self.subTest(extension=extension):
                self.assertEqual(self.classify(source, entry, route, enabled=False)['state'], 'same_revision')
                self.assertEqual(self.classify(source, entry, route)['state'], 'profile_outdated')
                for profile, expected in (('web-utf8-typography-v1', 'profile_outdated'),
                                          (WEB_PROFILE, 'same_revision'),
                                          ('web-utf8-typography-v2', 'profile_outdated'),
                                          ('web-utf8-typography-v3', 'profile_outdated'),
                                          ('web-utf8-typography-v5', 'same_revision'),
                                          ('other-profile', 'same_revision')):
                    self.assertEqual(self.classify(source, {**entry, 'profile': profile}, route)['state'], expected)
        for extension, kind in (('xlsx', 'spreadsheet-html-stream-index'), ('xlsx', WEB_INDEX),
                                ('html', 'spreadsheet-html-stream-index')):
            source, entry, route = web_row('foreign', extension)
            self.assertEqual(self.classify(source, entry, route, kind=kind)['state'], 'same_revision')

    def test_mapped_canonical_row_requires_exact_identity_and_primary_route(self):
        source, entry, route = web_row('book')
        for bad in ({'repo': 'VoiceOfML/Other'}, {'path': 'other.html'}, {'extension': 'htm'},
                    {'mode': 'fallback'}, {'source_sha256': 'not-pinned'}, {'sha256': 'not-pinned'},
                    {'bytes': True}, {'manifest': entry['object']}, {'profile': None}):
            with self.subTest(entry=bad):
                self.assertEqual(self.classify(source, {**entry, **bad}, route)['state'],
                                 'route_or_index_mismatch')
        for bad in ({'s': 1}, {'m': 'p'}, {'p': entry['manifest']},
                    {'p': 'other', 'o': entry['object']}, {'b': backlog.reader_lifecycle.PDF_PAGES_BUCKET},
                    {'f': 'fallback'}):
            with self.subTest(route=bad):
                self.assertEqual(self.classify(source, entry, {**route, **bad})['state'],
                                 'route_or_index_mismatch')
        self.assertEqual(self.classify(source, entry, None)['state'], 'not_mapped')
        self.assertEqual(self.classify(source, None, route)['state'], 'route_or_index_mismatch')

    def test_only_equal_current_source_bytes_qualify_for_profile_rebuild(self):
        source, entry, route = web_row('book', revision=OLD)
        path = source['path']
        for values, expected in (
                ({}, 'source_identity_unknown'),
                ({(NEW, path): metadata(path, SOURCE)}, 'profile_outdated'),
                ({(NEW, path): metadata(path, CHANGED)}, 'content_changed'),
                ({(NEW, path): metadata(path, blob=BLOB), (OLD, path): metadata(path, blob=BLOB)},
                 'profile_outdated'),
                ({(NEW, path): metadata(path, blob=BLOB), (OLD, path): metadata(path, SOURCE, BLOB)},
                 'source_identity_unknown')):
            with self.subTest(values=values):
                self.assertEqual(self.classify(source, entry, route, values=values)['state'], expected)

    def test_profile_failure_holds_exact_source_without_counting_as_ordinary_failure(self):
        source, entry, route = web_row('book', revision=OLD)
        path = source['path']
        failure = {'key': source['key'], 'source_revision': OLD, 'profile': WEB_PROFILE, 'source_sha256': SOURCE}
        for digest, override, state, deferred, recorded in (
                (SOURCE, {}, 'content_unchanged', True, False),
                (SOURCE, {'source_revision': NEW}, 'content_unchanged', True, False),
                (SOURCE, {'source_sha256': CHANGED}, 'profile_outdated', False, False),
                (SOURCE, {'profile': 'web-utf8-typography-v1'}, 'profile_outdated', False, False),
                (SOURCE, {'key': 'other'}, 'profile_outdated', False, False),
                (CHANGED, {}, 'content_changed', False, False),
                (CHANGED, {'source_revision': NEW}, 'content_changed', False, False),
                (CHANGED, {'source_revision': NEW, 'profile': None, 'source_sha256': None},
                 'content_changed', False, True)):
            with self.subTest(digest=digest, failure=override):
                result = self.classify(source, entry, route, values={(NEW, path): metadata(path, digest)},
                                       failures=[{**failure, **override}])
                self.assertEqual((result['state'], result.get('profile_upgrade_deferred', False),
                                  result['recorded_failure']), (state, deferred, recorded))
                if deferred:
                    self.assertEqual(result['profile_upgrade_deferred_reason'], 'current_web_profile_failed')


class WebProfileSourceSelectionTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.search_data, self.revisions, self.output, self.proof = (
            root / 'search.json', root / 'revisions.json', root / 'subset.json', root / 'proof' / 'web.json')
        self.pins = {REPO: NEW}
        self.revisions.write_text(json.dumps(self.pins))
        self.rows = {name: web_row(name, revision=OLD if name in {'changed', 'failed'} else NEW)
                     for name in ('upgrade', 'new', 'changed', 'failed')}
        current = web_row('current', profile=WEB_PROFILE)
        missing = web_row('missing', 'htm')
        self.rows.update(current=current, missing=missing)
        corpus = {'v': 2, 'rp': [REPO], 'fd': [[]], 'rc': [[0, name, src['extension'], 0, 1, 0]
                  for name, (src, _entry, _route) in self.rows.items()]}
        self.search_data.write_text(json.dumps(corpus))
        routes = {'f': {src['key']: route for name, (src, _entry, route) in self.rows.items() if name != 'new'}}
        failed = self.rows['failed'][0]
        failure = {'key': failed['key'], 'source_revision': OLD, 'profile': WEB_PROFILE, 'source_sha256': SOURCE}
        indexes = {'documents/web/html/index.json': index(
                       *(entry for name, (_src, entry, _route) in self.rows.items() if name not in {'new', 'missing'}),
                       failures=[failure]),
                   'chapters/ebook/epub/index.json': index(kind='ebook-chapter-stream-index'),
                   'pages/image/index-v2.json': index({'key': 'image'}, kind='image-stream-index')}
        pointer = make_pointer(SEARCH, None, '6' * 32)
        self.hub = Mock()
        self.hub.read_bytes.side_effect = lambda _bucket, path: pointer if path == backlog.CURRENT else b'manifest'
        self.api = Mock()
        self.api.repo_info.return_value = SimpleNamespace(sha=NEW)
        self.digests = {'changed.html': CHANGED}
        self.api.get_paths_info.side_effect = lambda _repo, paths, **_kw: [
            metadata(path, self.digests.get(path, SOURCE)) for path in paths]
        stack.enter_context(patch.object(backlog, 'capture_search', return_value=(pointer, b'manifest', corpus, self.pins, {})))
        stack.enter_context(patch.object(backlog.publication, 'current', return_value=(
            {'generation': READER}, {'base_sidecar': True})))
        stack.enter_context(patch.object(backlog.publication, 'pinned_base', return_value=routes))
        stack.enter_context(patch.object(backlog.publication, 'project_sidecar', return_value=routes))
        stack.enter_context(patch.object(backlog.publication, 'observe_consumer', return_value={
            'generation': READER, 'projection_verified': True}))
        stack.enter_context(patch.object(backlog.matrix, 'read_json', side_effect=lambda _fs, _bucket, path: indexes.get(path, {})))

    def prepare(self, **options):
        return sources.prepare(extension=options.pop('extension', 'html'), generation=SEARCH,
                               search_data=self.search_data, revisions=self.revisions, output=self.output,
                               hub=self.hub, api=self.api, fs=Mock(), client=Mock(), **options)

    def test_hold_one_failure_without_blocking_other_actionable_keys(self):
        summary, _ = backlog.inspect(hub=self.hub, api=self.api, fs=Mock(), client=Mock(),
                                     include_web_profile_upgrades=True)
        row = next(row for row in summary['formats'] if row['extension'] == 'html')
        self.assertEqual(row['profile_upgrade_deferred'], 1)
        self.assertEqual(row['profile_outdated'], 1)
        self.assertEqual(row['recorded_failure_current'], 0)
        self.assertEqual(row['definite_backlog'], 3)
        self.assertTrue(row['all_source_identities_checked'])

    def test_mix_profile_with_ordinary_work_and_emit_exact_old_route_proof(self):
        result = self.prepare(profile_proof=self.proof, include_profile_upgrades=False, include_web_profile_upgrades=True)
        self.assertEqual(result['selected_records'], 3)
        actual = backlog.source_records(json.loads(self.output.read_text()), self.pins)
        self.assertEqual(set(actual), {self.rows[name][0]['key'] for name in ('upgrade', 'new', 'changed')})
        source, entry, _route = self.rows['upgrade']
        self.assertEqual(json.loads(self.proof.read_text()), {
            'version': 1, 'extension': 'html', 'search_generation': SEARCH, 'reader_generation': READER,
            'profile_keys': {source['key']: {key: entry[key] for key in ('source_sha256', 'object', 'manifest', 'sha256', 'bytes')}}})

    def test_off_by_default_and_missing_index_never_yields_profile_work(self):
        self.assertEqual(self.prepare(profile_proof=self.proof)['selected_records'], 2)
        self.assertEqual(json.loads(self.proof.read_text())['profile_keys'], {})
        self.output = self.output.with_name('htm.json')
        self.proof = self.proof.with_name('htm.json')
        self.assertEqual(self.prepare(extension='htm', profile_proof=self.proof,
                                      include_web_profile_upgrades=True)['selected_records'], 0)
        self.assertEqual(json.loads(self.proof.read_text())['profile_keys'], {})

    def test_proof_must_be_new_distinct_and_present_when_profile_work_is_selected(self):
        self.proof.parent.mkdir()
        self.proof.symlink_to(self.output)
        for proof in (self.search_data, self.revisions, self.output, self.proof):
            with self.subTest(proof=proof), self.assertRaisesRegex(ValueError, 'distinct'):
                self.prepare(profile_proof=proof, include_web_profile_upgrades=True)
        with self.assertRaisesRegex(ValueError, 'require --profile-proof'):
            self.prepare(include_web_profile_upgrades=True)
        self.assertFalse(self.output.exists())

    def test_cli_opt_ins_are_independent(self):
        source_args = ['--extension', 'html', '--generation', SEARCH, '--search-data', str(self.search_data),
                       '--revisions', str(self.revisions), '--output', str(self.output), '--profile-proof', str(self.proof)]
        report_args = ['--output', str(self.output), '--epub-details', str(self.output.with_name('epub.json'))]
        with patch.dict(os.environ, {'READER_WEB_PROFILE_REBUILD_ENABLED': 'true',
                                     'READER_CHM_PROFILE_REBUILD_ENABLED': 'false'}, clear=True), \
                patch.object(backlog, 'inspect', return_value=({}, {})) as inspect, \
                patch.object(sources, 'prepare', return_value={}) as prepare, \
                patch.object(backlog, 'HubBucketStore'), patch.object(backlog, 'HfApi'), \
                patch.object(backlog, 'HfFileSystem'), patch.object(backlog.httpx, 'Client'), patch('builtins.print'):
            self.assertEqual(sources.main(source_args), 0)
            self.assertEqual(backlog.main(report_args), 0)
        for entry_point in (inspect, prepare):
            self.assertIs(entry_point.call_args.kwargs['include_web_profile_upgrades'], True)
            self.assertIs(entry_point.call_args.kwargs['include_profile_upgrades'], False)

    def test_producer_selection_checks_current_lfs_sha_even_on_same_revision(self):
        self.digests['upgrade.html'] = CHANGED
        with self.assertRaisesRegex(ValueError, 'differs from pinned source SHA-256'):
            self.prepare(profile_proof=self.proof, include_web_profile_upgrades=True)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.proof.exists())


if __name__ == '__main__':
    unittest.main()
