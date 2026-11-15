"""Automatic batches must progress past revision churn to actionable source keys."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import reader_format_sources as subject
from scripts import reader_assets, reader_stream_index, search_publication_pointer

backlog = subject.backlog
REPO, OTHER = 'VoiceOfML/Test', 'VoiceOfML/Other'
OLD, NEW, GENERATION = 'a' * 40, 'b' * 40, '1' * 64
SHA_A, SHA_B = '2' * 64, '3' * 64


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source, self.revisions, self.output = [self.root / name for name in ('search.json', 'commits.json', 'filtered.json')]
        self.pins = {REPO: NEW, OTHER: OLD}
        self.revisions.write_text(json.dumps(self.pins))
        self.rows, self.routes, self.indexes, self.metadata = [], {}, {}, {}
        self.pointer = search_publication_pointer.make_pointer(GENERATION, None, '4' * 32)
        self.reader_pointer = {'generation': '5' * 64}
        self.hub = Mock()
        self.hub.read_bytes.side_effect = lambda bucket, path: self.pointer if path == backlog.CURRENT else b'manifest'
        self.api = Mock()
        self.api.repo_info.side_effect = lambda repo, **kw: SimpleNamespace(sha=self.pins[repo])
        self.api.get_paths_info.side_effect = lambda repo, paths, **kw: [
            self.metadata[(repo, kw['revision'], path)] for path in paths
            if (repo, kw['revision'], path) in self.metadata]
        self.indexes['chapters/ebook/epub/index.json'] = self.index('ebook-chapter-stream-index')
        self.indexes['pages/image/index-v2.json'] = {
            'version': 1, 'kind': 'image-stream-index', 'files': [{'key': 'historical-image'}]}
        self.stack.enter_context(patch.object(backlog.publication, 'current', return_value=(
            self.reader_pointer, {'base_sidecar': {'sha256': '6' * 64}})))
        self.stack.enter_context(patch.object(backlog.publication, 'pinned_base', return_value={'f': self.routes}))
        self.stack.enter_context(patch.object(backlog.publication, 'project_sidecar', return_value={'f': self.routes}))
        self.stack.enter_context(patch.object(backlog.publication, 'observe_consumer', return_value={
            **self.reader_pointer, 'projection_verified': True}))
        self.stack.enter_context(patch.object(backlog.matrix, 'read_json', side_effect=lambda fs, bucket, path: self.indexes.get(path, {})))
        self.capture = self.stack.enter_context(patch.object(backlog, 'capture_search', side_effect=self.capture_search))

    @staticmethod
    def index(kind):
        return {'version': 1, 'kind': kind, 'files': [], 'failures': []}

    def capture_search(self, *args, **kwargs):
        return self.pointer, b'manifest', self.published, self.pins, {}

    def add(self, name, ext='docx', state='new'):
        key, path = REPO + '\0' + name + '.' + ext, name + '.' + ext
        self.rows.append([0, name, ext, 0, 1, 0])
        metadata = SimpleNamespace(path=path, lfs={'sha256': SHA_A if state == 'unchanged' else SHA_B}, blob_id=None)
        self.metadata[(REPO, NEW, path)] = metadata
        if ext in {'docx', 'epub'}:
            index_path, kind, mode = 'chapters/ebook/epub/index.json', 'ebook-chapter-stream-index', 'e'
        elif ext == 'xlsx':
            index_path, kind, mode = 'documents/spreadsheet/xlsx/index.json', 'spreadsheet-html-stream-index', 'h'
        else:
            index_path, kind, mode = f'documents/web/{ext}/index.json', 'web-document-stream-index', 'h'
        index = self.indexes.setdefault(index_path, self.index(kind))
        entry = {'key': key, 'source_revision': NEW if state == 'same' else OLD,
                 'source_sha256': SHA_A, 'source_extension': ext, 'bucket': backlog.READER_BUCKET,
                 'manifest': f'chapters/ebook/{ext}/{name}/chapter-manifest.json'}
        if state in {'unchanged', 'changed', 'same', 'unknown', 'mismatch'}:
            index['files'].append(entry)
            self.routes[key] = {'s': 2, 'm': mode, 'b': backlog.READER_BUCKET, 'p': entry['manifest']}
        if state == 'unknown':
            del self.metadata[(REPO, NEW, path)]
        if state == 'mismatch':
            self.routes[key]['p'] = 'other'
        if state == 'failure':
            index['failures'].append({'key': key, 'source_revision': NEW})
        if state == 'pending':
            number = reader_stream_index.shard_for(key, 8)
            shard = self.index(kind)
            shard['files'].append({**entry, 'source_revision': NEW})
            self.indexes[f'chapters/ebook/epub/index-{number:02d}.json'] = shard
        return key

    def write_source(self):
        self.payload = {'v': 2, 'rp': [REPO, OTHER], 'fd': [[], ['unused']], 'rc': self.rows}
        self.published = copy.deepcopy(self.payload)
        # Normal fetch omits sizes, publication may normalize the OCR bit.
        for row in self.published['rc']:
            row[4], row[5] = 100, 1
        self.source.write_text(json.dumps(self.payload))

    def prepare(self, extension='docx', generation=GENERATION):
        return subject.prepare(extension=extension, generation=generation, search_data=self.source,
                               revisions=self.revisions, output=self.output, hub=self.hub,
                               api=self.api, fs=Mock(), client=Mock())

    def test_revision_churn_before_ten_new_sources_filters_by_key_for_office_and_sheet(self):
        for ext in ('docx', 'xlsx'):
            for number in range(12):
                self.add('a-unchanged-' + str(number), ext, 'unchanged')
            for number in range(10):
                self.add('z-new-' + str(number), ext)
            self.add('changed', ext, 'changed')
        self.write_source()
        for ext in ('docx', 'xlsx'):
            with self.subTest(ext=ext):
                self.output = self.root / (ext + '.json')
                report = self.prepare(ext)
                filtered = json.loads(self.output.read_text())
                self.assertEqual(report['selected_records'], 11)
                self.assertEqual(filtered['rp'], self.payload['rp'])
                self.assertEqual(filtered['fd'], self.payload['fd'])
                decoded = reader_assets.decode_search_payload(filtered)
                self.assertEqual({r['Extension'] for r in decoded}, {ext})
                self.assertEqual({r['File'] for r in decoded}, {'changed'} | {'z-new-' + str(n) for n in range(10)})
                planned = backlog.matrix.selected_records(
                    self.output, self.pins, 'office-chapters' if ext == 'docx' else 'spreadsheet', {ext})
                self.assertEqual(len(planned), 11)
                self.assertTrue(all(r['revision'] == NEW for r in planned))
                self.assertFalse(any('a-unchanged-' in r['key'] for r in planned))
        self.assertEqual(self.capture.call_count, 2)  # Exactly one full inspect per prepare.
        self.assertEqual(json.loads(self.revisions.read_text()), self.pins)
        self.assertEqual(json.loads(self.source.read_text()), self.payload)

    def test_failure_pending_unknown_mismatch_same_and_missing_index_are_excluded(self):
        for state in ('failure', 'unknown', 'mismatch', 'same', 'unchanged', 'new', 'changed'):
            self.add(state, state=state)
        self.add('pending', 'epub', 'pending')
        self.add('unverified', 'xlsx')
        del self.indexes['documents/spreadsheet/xlsx/index.json']
        self.write_source()
        self.prepare()
        self.assertEqual([r['File'] for r in reader_assets.decode_search_payload(json.loads(self.output.read_text()))], ['new', 'changed'])
        self.output = self.root / 'empty.json'
        self.assertEqual(self.prepare('xlsx')['selected_records'], 0)
        self.assertEqual(reader_assets.decode_search_payload(json.loads(self.output.read_text())), [])
        self.output = self.root / 'pending.json'
        self.assertEqual(self.prepare('epub')['selected_records'], 0)

    def test_web_new_sources_only_without_claiming_dependency_freshness(self):
        for ext in ('html', 'mhtml'):
            self.add('new', ext)
            self.add('changed', ext, 'changed')
            self.add('unchanged', ext, 'unchanged')
        self.write_source()
        for ext in ('html', 'mhtml'):
            self.output = self.root / (ext + '.json')
            self.assertEqual(self.prepare(ext)['selected_records'], 1)
            self.assertEqual(json.loads(self.output.read_text())['rc'], [[0, 'new', ext, 0, 1, 0]])
        summary, _ = backlog.inspect(hub=self.hub, api=self.api, fs=Mock(), client=Mock())
        self.assertFalse(next(r for r in summary['formats'] if r['extension'] == 'html')['conversion_dependencies_checked'])

    def test_full_inventory_and_pins_must_match_before_any_output(self):
        self.add('new')
        self.add('other-format', 'xlsx')
        self.write_source()
        original = self.source.read_bytes()
        for mutation in ('missing', 'extra', 'path', 'revision', 'generation', 'head'):
            with self.subTest(mutation=mutation):
                payload = copy.deepcopy(self.payload)
                if mutation == 'missing': payload['rc'].pop()
                if mutation == 'extra': payload['rc'].append([0, 'extra', 'docx', 0, 1, 0])
                if mutation == 'path': payload['fd'][0] = ['different']
                self.source.write_text(json.dumps(payload))
                self.revisions.write_text(json.dumps({**self.pins, REPO: OLD} if mutation == 'revision' else self.pins))
                self.api.repo_info.side_effect = lambda repo, **kw: SimpleNamespace(sha=OLD if mutation == 'head' else self.pins[repo])
                with self.assertRaises(ValueError):
                    self.prepare(generation='f' * 64 if mutation == 'generation' else GENERATION)
                self.assertFalse(self.output.exists())
        self.source.write_bytes(original)

    def test_selected_not_mapped_requires_exact_metadata_hash(self):
        self.add('new')
        self.write_source()
        for value in (None, SimpleNamespace(path='new.docx', lfs=None, blob_id=None),
                      SimpleNamespace(path='new.docx', lfs={'sha256': 'bad'}, blob_id=OLD),
                      SimpleNamespace(path='foreign.docx', lfs=None, blob_id=OLD)):
            self.metadata.clear()
            if value is not None: self.metadata[(REPO, NEW, 'new.docx')] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse(self.output.exists())
        self.metadata[(REPO, NEW, 'new.docx')] = SimpleNamespace(path='new.docx', lfs=None, blob_id=OLD)
        self.assertEqual(self.prepare()['selected_records'], 1)
        self.assertTrue(all(call.kwargs['revision'] == NEW for call in self.api.get_paths_info.call_args_list))

    def test_details_out_preserves_pair_and_contains_unchanged_rows_after_checks(self):
        key = self.add('unchanged', state='unchanged')
        self.write_source()
        details = {}
        result = backlog.inspect(hub=self.hub, api=self.api, fs=Mock(), client=Mock(), details_out=details)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]['kind'], 'reader-epub-content-backlog')
        self.assertEqual(details['rows'][key]['state'], 'content_unchanged')
        self.assertEqual(details['records'][key]['revision'], NEW)
        self.assertEqual(details['corpus'], self.published)
        self.api.repo_info.side_effect = lambda *a, **kw: SimpleNamespace(sha=OLD)
        untouched = {'sentinel': True}
        with self.assertRaises(ValueError):
            backlog.inspect(hub=self.hub, api=self.api, fs=Mock(), client=Mock(), details_out=untouched)
        self.assertEqual(untouched, {'sentinel': True})

    def test_refuses_overwrite_symlink_and_invalid_or_duplicate_corpus(self):
        self.add('new')
        self.write_source()
        for output in (self.source, self.revisions):
            self.output = output
            with self.assertRaisesRegex(ValueError, 'new distinct path'): self.prepare()
        self.output = self.root / 'symlink.json'
        self.output.symlink_to(self.root / 'absent.json')
        with self.assertRaisesRegex(ValueError, 'new distinct path'): self.prepare()
        self.output = self.root / 'filtered.json'
        for payload in ([], {**self.payload, 'rc': self.rows * 2}):
            self.source.write_text(json.dumps(payload))
            with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(self.output.exists())
        self.capture.assert_not_called()

    def test_cli_writes_report_and_converter_input(self):
        self.add('new')
        self.write_source()
        with patch.object(backlog, 'HubBucketStore', return_value=self.hub), \
                patch.object(backlog, 'HfApi', return_value=self.api), \
                patch.object(backlog, 'HfFileSystem'), patch.object(backlog.httpx, 'Client'), \
                patch('builtins.print') as printed:
            self.assertEqual(subject.main(['--extension', 'docx', '--generation', GENERATION,
                                          '--search-data', str(self.source), '--revisions', str(self.revisions),
                                          '--output', str(self.output)]), 0)
        report = json.loads(printed.call_args.args[0])
        self.assertEqual(report['output'], str(self.output))
        self.assertEqual(report['selected_records'], 1)
        self.assertEqual(report['remote_writes'], 0)


if __name__ == '__main__':
    unittest.main()
