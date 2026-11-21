"""Automatic batches must progress past revision churn to actionable source keys."""
import base64
import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import reader_format_sources as subject
from scripts import reader_assets, reader_stream_index, search_publication_pointer
from scripts import pilot_text_stream as text, pilot_web_stream as web
from scripts import pilot_image_page_stream as image, pilot_media_stream as media
from scripts import convert_reader_assets as converter

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
            'version': 1, 'kind': 'image-stream-index', 'files': [{'key': 'historical-image'}], 'failures': []}
        self.catalog = {'base_sidecar': {'sha256': '6' * 64}}
        self.stack.enter_context(patch.object(backlog.publication, 'current', return_value=(
            self.reader_pointer, self.catalog)))
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

    def add(self, name, ext='docx', state='new', *, profile=None):
        key, path = REPO + '\0' + name + '.' + ext, name + '.' + ext
        self.rows.append([0, name, ext, 0, 1, 0])
        metadata = SimpleNamespace(path=path, lfs={'sha256': SHA_A if state == 'unchanged' else SHA_B}, blob_id=None)
        self.metadata[(REPO, NEW, path)] = metadata
        if ext in {'docx', 'epub', 'chm'}:
            chapter_ext = 'chm' if ext == 'chm' else 'epub'
            index_path, kind, mode = f'chapters/ebook/{chapter_ext}/index.json', 'ebook-chapter-stream-index', 'e'
        elif ext == 'jpg':
            index_path, kind, mode = 'pages/image/index-v2.json', 'image-stream-index', 'i'
        elif ext == 'mp3':
            index_path, kind, mode = 'media/audio/index.json', 'media-stream-index', 'a'
        elif ext in {'md', 'markdown'}:
            index_path, kind, mode = 'documents/text/index.json', 'text-stream-index', 'k'
        elif ext == 'xlsx':
            index_path, kind, mode = 'documents/spreadsheet/xlsx/index.json', 'spreadsheet-html-stream-index', 'h'
        else:
            index_path, kind, mode = f'documents/web/{ext}/index.json', 'web-document-stream-index', 'h'
        index = self.indexes.setdefault(index_path, self.index(kind))
        entry = {'key': key, 'source_revision': NEW if state == 'same' else OLD,
                 'source_sha256': SHA_A, 'source_extension': ext, 'bucket': backlog.READER_BUCKET,
                 'manifest': f'chapters/ebook/{ext}/{name}/chapter-manifest.json'}
        if profile is not None:
            entry['profile'] = profile
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

    def prepare(self, extension='docx', generation=GENERATION, **options):
        return subject.prepare(extension=extension, generation=generation, search_data=self.source,
                               revisions=self.revisions, output=self.output, hub=self.hub,
                               api=self.api, fs=Mock(), client=Mock(), **options)

    def test_withdrawn_source_is_not_reconverted_even_with_threshold_one(self):
        withdrawn = self.add('intentionally-withdrawn')
        remaining = self.add('actual-new')
        self.catalog['suppressed_sources'] = [withdrawn]
        self.write_source()
        report = self.prepare()
        self.assertEqual(report['source_records'], 2)
        self.assertEqual(report['selected_records'], 1)
        self.assertEqual(set(backlog.source_records(json.loads(self.output.read_text()), self.pins)), {remaining})

    def test_reviewed_chm_profile_enters_queue_without_source_change(self):
        legacy = 'chm-epub-chapters-v9-source-text'
        current = 'chm-epub-chapters-v10-exact-sections'
        self.add('same-revision', 'chm', 'same', profile=legacy)
        self.add('same-content', 'chm', 'unchanged', profile=legacy)
        self.add('current', 'chm', 'same', profile=current)
        self.add('missing-profile', 'chm', 'same')
        self.add('unknown-profile', 'chm', 'same', profile='unreviewed')
        self.add('unknown-source', 'chm', 'unknown', profile=legacy)
        self.add('wrong-route', 'chm', 'mismatch', profile=legacy)
        broken = self.add('previous-failure', 'chm', 'same', profile=legacy)
        legacy_index = self.indexes['chapters/ebook/chm/index.json']
        legacy_index['failures'].append({'key': broken, 'source_revision': NEW})
        stale_failure = self.add('previous-v10-failure', 'chm', 'unchanged', profile=legacy)
        legacy_index['failures'].append({'key': stale_failure, 'source_revision': OLD, 'profile': current})
        with_fallback = self.add('fallback-preserved', 'chm', 'same', profile=legacy)
        next(e for e in legacy_index['files'] if e['key'] == with_fallback)['fallback'] = 'native/ebook/chm/fallback.epub'
        historical_match = self.add('fallback-matches-wrong-chapter', 'chm', 'same', profile=legacy)
        e = next(e for e in legacy_index['files'] if e['key'] == historical_match)
        e['object'] = self.routes[historical_match]['p']
        self.routes[historical_match].update(c='chapters/ebook/chm/different/chapter-manifest.json', cb=backlog.READER_BUCKET)
        self.write_source()
        proof_file = self.root / 'chm-proof.json'
        report = self.prepare('chm', profile_proof=proof_file)
        self.assertEqual(report['selected_records'], 2)
        payload = json.loads(self.output.read_text())
        self.assertEqual({r['File'] for r in reader_assets.decode_search_payload(payload)},
                         {'same-revision', 'same-content'})
        proof = json.loads(proof_file.read_text())
        self.assertEqual(proof['search_generation'], GENERATION)
        self.assertEqual(proof['reader_generation'], self.reader_pointer['generation'])
        self.assertEqual(proof['profile_keys'], {REPO + '\0' + name + '.chm': SHA_A
                                                for name in ('same-revision', 'same-content')})
        self.output = self.root / 'profile-disabled.json'
        new_key = self.add('actual-new', 'chm')
        self.write_source()
        self.assertEqual(self.prepare('chm', include_profile_upgrades=False)['selected_records'], 1)
        self.assertEqual(set(backlog.source_records(json.loads(self.output.read_text()), self.pins)), {new_key})

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

    def test_real_48_new_39_changed_filter_keeps_only_known_source_changes(self):
        extensions = ('md', 'markdown', 'html', 'htm', 'mhtml', 'mht')
        for ext in extensions:
            for state, count in (('new', 48), ('changed', 39), ('unchanged', 13)):
                for number in range(count):
                    self.add(f'{state}-{number:02d}', ext, state)
        self.write_source()
        summary, _ = backlog.inspect(hub=self.hub, api=self.api, fs=Mock(), client=Mock())
        for ext in extensions:
            with self.subTest(ext=ext):
                row = next(row for row in summary['formats'] if row['extension'] == ext)
                self.assertEqual((row['not_mapped'], row['source_content_changed']), (48, 39))
                self.assertEqual(row['source_content_unchanged'], 13)
                self.assertTrue(row['all_source_identities_checked'])
                self.assertFalse(row['conversion_dependencies_checked'])
                self.output = self.root / (ext + '.json')
                self.assertEqual(self.prepare(ext)['selected_records'], 87)
                decoded = reader_assets.decode_search_payload(json.loads(self.output.read_text()))
                self.assertEqual({r['File'] for r in decoded},
                                 {f'new-{n:02d}' for n in range(48)} | {f'changed-{n:02d}' for n in range(39)})
                selected = (text.select_records(self.output, self.pins, ext, all_markdown_repos=True)
                            if ext in {'md', 'markdown'} else web.select_records(self.output, self.pins, ext))
                self.assertEqual(len(selected), 87)
                self.assertTrue(all(item['revision'] == NEW for item in selected))

    def test_changed_markdown_and_html_builds_recapture_current_dependencies(self):
        for ext in ('md', 'markdown', 'html', 'htm'):
            self.add('changed', ext, 'changed')
        self.write_source()
        for ext in ('md', 'markdown', 'html', 'htm'):
            with self.subTest(ext=ext):
                self.output = self.root / (ext + '.json')
                self.prepare(ext)
                markdown = ext in {'md', 'markdown'}
                selected = (text.select_records(self.output, self.pins, ext, all_markdown_repos=True)
                            if markdown else web.select_records(self.output, self.pins, ext))
                item = selected[0]
                builds = []
                for revision in (OLD, NEW):
                    image = b'\x89PNG\r\n\x1a\n' + revision.encode()
                    raw = ((f'# Changed {revision}\n![figure](figure.png)\n') if markdown else
                           f'<p>Changed {revision}</p><img src="figure.png">').encode()
                    urls = []
                    def download(url, target, *args, **kwargs):
                        urls.append(url)
                        data = image if url.endswith('.png') else raw
                        target.write_bytes(data)
                        return hashlib.sha256(data).hexdigest(), len(data)
                    with patch.object(text if markdown else converter,
                                      'download' if markdown else 'download_source', side_effect=download), \
                            patch.object(web, 'download_source', side_effect=download):
                        builder = text if markdown else web
                        entry, files = builder.build_one({**item, 'revision': revision},
                            self.root / (ext + revision), None, backlog.READER_BUCKET)
                    output = Path(files[entry['object']]).read_bytes()
                    self.assertIn(base64.b64encode(image), output)
                    self.assertEqual(entry['source_sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertTrue(all('/' + revision + '/' in url for url in urls))
                    self.assertEqual(len(urls), 2)
                    builds.append(entry)
                self.assertNotEqual(builds[0]['object'], builds[1]['object'])
                self.assertEqual(builds[1]['source_revision'], NEW)

    def test_dependency_read_errors_fail_build_and_strict_web_batch(self):
        item = {'repo': REPO, 'path': 'changed.html', 'extension': 'html', 'revision': NEW}
        def download(url, target, **kwargs):
            data = b'<p>Changed body</p><img src="figure.png">' if url.endswith('.html') else b'PNG'
            target.write_bytes(data)
            return hashlib.sha256(data).hexdigest(), len(data)
        original_read = Path.read_bytes
        def read(path):
            if 'html-resources' in path.parts:
                raise OSError('dependency read failed')
            return original_read(path)
        with patch.object(converter, 'download_source', side_effect=download), \
                patch.object(web, 'download_source', side_effect=download), \
                patch.object(Path, 'read_bytes', read), \
                patch.object(reader_stream_index, 'read_index', return_value=self.index('web-document-stream-index')), \
                patch.object(reader_stream_index, 'publish_checkpoint') as checkpoint:
            with self.assertRaisesRegex(OSError, 'dependency read failed'):
                web.build_one(item, self.root / 'failed-web', None, backlog.READER_BUCKET)
            status = reader_stream_index.run_documents([item], bucket=backlog.READER_BUCKET,
                token=None, limit=10, retry_failures=False, strict=True, apply=False,
                prefix='documents/web', kind='web-document-stream-index', build=web.build_one,
                work=self.root / 'strict-web')
            self.assertEqual(status, 1)
            self.assertEqual(checkpoint.call_args.args[3], [])
            self.assertEqual(len(checkpoint.call_args.args[4]), 1)
            self.assertEqual(checkpoint.call_args.args[5], {})
        def markdown_download(url, target, token, **kwargs):
            if url.endswith('.png'):
                raise OSError('dependency read failed')
            target.write_bytes(b'# Changed body\n![figure](figure.png)\n')
        with patch.object(text, 'download', side_effect=markdown_download):
            with self.assertRaisesRegex((ValueError, OSError), 'dependency read failed'):
                text.build_one({**item, 'path': 'changed.md', 'extension': 'md'},
                               self.root / 'failed-md', None, backlog.READER_BUCKET)

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

    def boost_source(self, name_prefix, number, ext, state, size, shard):
        for candidate in range(1000):
            name = f'{name_prefix}-{number:02d}-{candidate}'
            path = f'{name}.{ext}'
            if reader_stream_index.shard_for(f'{REPO}\0{path}', 8) == shard:
                break
        else:
            self.fail('could not find a fixture key for shard')
        key = self.add(name, ext, state)
        self.metadata[(REPO, NEW, path)].size = size
        return key

    def test_boosted_subset_follows_both_workers_order_and_budgets_without_filling_from_exclusions(self):
        for extension, budget in (('jpg', 256 * 1024**2), ('mp3', 512 * 1024**2)):
            with self.subTest(extension=extension):
                start = len(self.rows)
                rows = []
                for n, size in enumerate((budget // 3, budget // 3, budget // 3, 3, 4)):
                    rows.append((self.boost_source('regular', n, extension, 'changed' if n == 1 else 'new', size, 2), size))
                changed = rows[1][0]
                other_keys = [self.boost_source('another-shard', n, extension, 'new', 10, 6) for n in reversed(range(6))]
                for state, size in (('new', 0), ('new', budget + 1), ('failure', 1), ('unchanged', 1)):
                    self.boost_source('000-' + state, size % 100, extension, state, size, 2)
                staged = self.boost_source('000-pending', 0, extension, 'new', 1, 2)
                index_root, index_base, kind = (('pages/image', 'index-v2', 'image-stream-index') if extension == 'jpg' else
                                               ('media/audio', 'index', 'media-stream-index'))
                shard_index = self.index(kind)
                shard_index['files'].append({'key': staged, 'extension': extension, 'source_revision': NEW})
                self.indexes[f'{index_root}/{index_base}-02.json'] = shard_index
                self.write_source()
                # The fresh fetch gives misleading sizes; pinned sizes must win both admission and worker ordering.
                sizes = {path: item.size for (repo, revision, path), item in self.metadata.items() if path.endswith('.' + extension)}
                for row in self.published['rc'][start:]:
                    row[4] = sizes[f'{row[1]}.{extension}']
                self.output = self.root / f'{extension}.json'
                report = self.prepare(extension, boost_limit=4)
                self.assertEqual(report['skipped_unknown_or_oversized'], 2)
                self.assertEqual(report['source_byte_budget_per_shard'], budget)
                workers = (image.source_records(self.output, self.pins, 0, 'jpg') if extension == 'jpg' else
                           media.select_records(self.output, self.pins, 'audio', 'mp3'))
                key_for = lambda row: f"{row['repo']}\0{row['path']}"
                size_field = 'bytes' if extension == 'jpg' else 'source_bytes'
                shards = {2: [], 6: []}
                for row in workers:
                    self.assertEqual(row[size_field], sizes[row['path']])
                    shard = (image if extension == 'jpg' else media).shard_for(key_for(row), 8)
                    shards[shard].append(row)
                expected = ([key for key, _ in sorted(rows, key=lambda pair: (pair[1], pair[0]))][:4]
                            if extension == 'jpg' else [key for key, _ in rows[:3]])
                self.assertEqual([key_for(row) for row in shards[2]], expected)
                self.assertIn(changed, expected)
                self.assertEqual([key_for(row) for row in shards[6]], sorted(other_keys)[:4])
                self.assertTrue(all(sum(row[size_field] for row in part) <= budget for part in shards.values()))
                self.assertEqual(json.loads(self.revisions.read_text()), self.pins)
                planner_kind, wanted = ('image', {'jpg'}) if extension == 'jpg' else ('media', {'audio'})
                planned = backlog.matrix.selected_records(self.output, self.pins, planner_kind, wanted,
                                                          'mp3' if extension == 'mp3' else 'all')
                self.assertEqual({row['key'] for row in planned}, {key_for(row) for row in workers})

    def test_boost_fails_closed_on_missing_or_mismatched_budget_metadata_without_legacy_fallback(self):
        self.boost_source('candidate', 0, 'jpg', 'new', 40, 3)
        self.write_source()
        for pinned_size in (0, 41):
            self.published['rc'][0][4] = pinned_size
            with self.assertRaisesRegex(ValueError, 'positive pinned size|differs from pinned file metadata'):
                self.prepare('jpg', boost_limit=4)
            self.assertFalse(self.output.exists())
        self.assertEqual(self.prepare('jpg')['selected_records'], 1)
        self.assertEqual(json.loads(self.output.read_text()), self.payload)

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
