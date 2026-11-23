from collections import Counter
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from scripts import plan_reader_stream_matrix as matrix, office_symbol_fonts
from scripts.plan_reader_stream_matrix import completed_entries, read_json, selected_records
from scripts.pilot_media_stream import select_records as select_media_records
from scripts.pilot_epub_chapters import shard_for as ebook_shard_for


def selected_records_fixture_image():
    rows = [{'Repo': 'VoiceOfML/Test', 'Folder': [], 'File': 'book', 'Extension': 'png', 'Size': 0}]
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / 'search.json'
        source.write_text(json.dumps(rows))
        return selected_records(source, {'VoiceOfML/Test': 'a' * 40}, 'image', {'png'})


class ReaderStreamMatrixIndexTests(unittest.TestCase):
    def test_only_a_missing_index_is_treated_as_empty(self):
        fs = Mock()
        fs.open.side_effect = FileNotFoundError("missing index")
        self.assertEqual(read_json(fs, "bucket", "chapters/ebook/epub/index.json"), {})

        for error in (PermissionError("denied"), OSError("network read failed")):
            fs = Mock()
            fs.open.side_effect = error
            with self.assertRaises(type(error)):
                read_json(fs, "bucket", "chapters/ebook/epub/index.json")

    def test_invalid_indexes_fail_closed(self):
        for raw in (b"[]", b"not-json", b'{"files":{}}', b'{"files":[],"failures":{}}'):
            fs = Mock()
            fs.open.return_value = io.BytesIO(raw)
            with self.assertRaises(ValueError):
                read_json(fs, "bucket", "chapters/ebook/epub/index.json")

    def test_failed_sources_are_skipped_unless_retry_is_explicit(self):
        index = {"files": [{"key": "good", "source_revision": "r1"}],
                 "failures": [{"key": "bad", "source_revision": "r2"}]}
        self.assertEqual(completed_entries(index), {"good": "r1", "bad": "r2"})
        self.assertEqual(completed_entries(index, retry_failures=True), {"good": "r1"})

    def test_static_pdf_planning_matches_converter_profile_and_failure_rules(self):
        key = 'VoiceOfML/Test\0book.pptx'
        candidate = {'key': key, 'extension': 'pptx', 'revision': 'rev'}
        cases = [
            ({'files': [], 'failures': []}, False, 1),
            ({'files': [{'key': key, 'source_revision': 'rev'}], 'failures': []}, False, 1),
            ({'files': [{'key': key, 'source_revision': 'rev',
                         'conversion_profile': office_symbol_fonts.PROFILE}], 'failures': []}, False, 0),
            ({'files': [], 'failures': [{'key': key, 'source_revision': 'rev'}]}, False, 1),
            ({'files': [], 'failures': [{'key': key, 'source_revision': 'rev',
                                        'conversion_profile': office_symbol_fonts.PROFILE}]}, False, 0),
            ({'files': [], 'failures': [{'key': key, 'source_revision': 'rev',
                                        'conversion_profile': office_symbol_fonts.PROFILE}]}, True, 1),
        ]
        for index, retry, expected in cases:
            with self.subTest(index=index, retry=retry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'revisions.json').write_text('{}')
                args = SimpleNamespace(kind='static-pdf', extensions=['pptx'], search_data=root / 'search.json',
                    revisions=root / 'revisions.json', bucket='bucket', shard_count=8,
                    output=root / 'matrix.json', retry_failures=retry, verify_source_sha=False,
                    report=root / 'report.json')
                with patch.object(matrix, 'args', return_value=args), \
                        patch.object(matrix, 'HfFileSystem'), \
                        patch.object(matrix, 'selected_records', return_value=[candidate]), \
                        patch.object(matrix, 'read_json', return_value=index):
                    self.assertEqual(matrix.main(), 0)
                self.assertEqual(len(json.loads(args.output.read_text())['include']), expected)
                self.assertEqual(json.loads(args.report.read_text())['extensions'][0]['pending'], expected)

    def test_static_pdf_plan_reuses_pinned_unchanged_source_without_hiding_profile_upgrade(self):
        key = 'VoiceOfML/Test\0book.ppt'
        candidate = {'key': key, 'extension': 'ppt', 'revision': 'new'}
        good = {'key': key, 'source_revision': 'old', 'source_sha256': 'a' * 64,
                'bucket': 'vomebook/reader-assets-v2', 'mode': 'pdf', 'sha256': 'b' * 64,
                'object': 'documents/pdf/ppt/object.pdf', 'manifest': 'documents/pdf/ppt/manifest.json'}
        api = SimpleNamespace(get_paths_info=lambda repo, paths, **kwargs:
            [SimpleNamespace(path='book.ppt', lfs={'sha256': 'a' * 64})])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'revisions.json').write_text('{}')
            args = SimpleNamespace(kind='static-pdf', extensions=['ppt'], search_data=root / 'search.json',
                revisions=root / 'revisions.json', bucket='bucket', shard_count=8,
                output=root / 'matrix.json', retry_failures=False, verify_source_sha=False,
                report=root / 'report.json')
            with patch.object(matrix, 'args', return_value=args), patch.object(matrix, 'HfFileSystem'), \
                    patch.object(matrix, 'selected_records', return_value=[candidate]), \
                    patch.object(matrix, 'read_json', return_value={'files': [good], 'failures': []}), \
                    patch('scripts.reader_stream_index.HfApi', return_value=api):
                self.assertEqual(matrix.main(), 0)
            self.assertEqual(json.loads(args.output.read_text())['include'], [])
            summary = json.loads(args.report.read_text())['extensions'][0]
            self.assertEqual((summary['pending'], summary['unchanged_sha']), (0, 1))

    def test_sharded_planner_never_hides_recorded_failure_behind_same_sha(self):
        key = 'VoiceOfML/Test\0bad.mp3'
        candidate = {'key': key, 'extension': 'audio', 'revision': 'new'}
        current = {'files': [{'key': key, 'source_revision': 'old', 'source_sha256': 'a' * 64}],
                   'failures': [{'key': key, 'source_revision': 'new'}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'revisions.json').write_text('{}')
            args = SimpleNamespace(kind='media', extensions=['audio'], search_data=root / 'search.json',
                revisions=root / 'revisions.json', bucket='bucket', shard_count=1,
                output=root / 'matrix.json', retry_failures=True, verify_source_sha=True,
                report=root / 'report.json')
            with patch.object(matrix, 'args', return_value=args), patch.object(matrix, 'HfFileSystem'), \
                    patch.object(matrix, 'selected_records', return_value=[candidate]), \
                    patch.object(matrix, 'read_json', side_effect=[current, {}]), \
                    patch.object(matrix, 'unchanged_sources', return_value=set()) as reuse:
                self.assertEqual(matrix.main(), 0)
            self.assertEqual(reuse.call_args.args[1]['files'], [])
            self.assertEqual(json.loads(args.output.read_text())['include'], [{'extension': 'audio', 'shard': 0}])

    def test_image_planner_uses_v2_index_and_never_treats_missing_size_as_convertible(self):
        self.assertEqual(selected_records_fixture_image(), [])
        key = 'VoiceOfML/Test\0book.png'
        candidate = {'key': key, 'extension': 'png', 'revision': 'new'}
        canonical = {'files': [{'key': key, 'source_revision': 'old', 'source_sha256': 'a' * 64}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'revisions.json').write_text('{}')
            args = SimpleNamespace(kind='image', extensions=['png'], search_data=root / 'search.json',
                revisions=root / 'revisions.json', bucket='bucket', shard_count=1,
                output=root / 'matrix.json', retry_failures=False, verify_source_sha=True, report=root / 'report.json')
            seen = []
            def read(_fs, _bucket, path):
                seen.append(path)
                return canonical if path.endswith('index-v2.json') else {}
            with patch.object(matrix, 'args', return_value=args), patch.object(matrix, 'HfFileSystem'), \
                    patch.object(matrix, 'selected_records', return_value=[candidate]), \
                    patch.object(matrix, 'read_json', side_effect=read), \
                    patch.object(matrix, 'unchanged_sources', return_value={key}):
                self.assertEqual(matrix.main(), 0)
            self.assertEqual(seen, ['pages/image/index-v2.json', 'pages/image/index-v2-00.json'])
            self.assertEqual(json.loads(args.output.read_text())['include'], [])

    def test_image_planner_limits_matrix_to_requested_shard(self):
        from scripts.reader_stream_index import shard_for
        candidates = [{'key': f'VoiceOfML/Test\0{i}.png', 'extension': 'png', 'revision': 'new'} for i in range(40)]
        canonical = {'files': [{'key': 'VoiceOfML/Test\0old.png', 'source_revision': 'old'}]}
        wanted = 3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'revisions.json').write_text('{}')
            args = SimpleNamespace(kind='image', extensions=['png'], search_data=root / 'search.json',
                revisions=root / 'revisions.json', bucket='bucket', shard_count=8, shard_index=wanted,
                output=root / 'matrix.json', retry_failures=False, verify_source_sha=False, report=root / 'report.json')
            def read(_fs, _bucket, path):
                return canonical if path.endswith('index-v2.json') else {}
            with patch.object(matrix, 'args', return_value=args), patch.object(matrix, 'HfFileSystem'), \
                    patch.object(matrix, 'selected_records', return_value=candidates), \
                    patch.object(matrix, 'read_json', side_effect=read):
                self.assertEqual(matrix.main(), 0)
            self.assertEqual(json.loads(args.output.read_text())['include'], [{'extension': 'png', 'shard': wanted}])
            self.assertEqual(json.loads(args.report.read_text())['extensions'][0]['pending'],
                             sum(shard_for(item['key'], 8) == wanted for item in candidates))

    def test_retry_failure_overrides_same_revision_success_in_planner(self):
        source = {'key': 'repo\0image.png', 'source_revision': 'same'}
        index = {'files': [source], 'failures': [dict(source)]}
        self.assertEqual(matrix.completed_entries(index, retry_failures=True), {})
        self.assertEqual(matrix.completed_entries(index, retry_failures=False), {source['key']: 'same'})

    def test_office_workflow_exposes_and_passes_explicit_failure_retry(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
                    "reader-office-chapters.yml").read_text(encoding="utf-8")
        self.assertIn("retry_failures:", workflow)
        self.assertEqual(workflow.count("--retry-failures"), 2)

    def test_aac_is_planned_and_selected_only_as_audio(self):
        rows = [{"Repo": "VoiceOfML/Test", "Folder": ["music"], "File": "clip",
                 "Extension": "aac", "Size": 123}]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "search.json"
            source.write_text(json.dumps(rows), encoding="utf-8")
            revisions = {"VoiceOfML/Test": "a" * 40}
            planned = selected_records(source, revisions, "media", {"audio"})
            converted = select_media_records(source, revisions, "audio")
            self.assertEqual([(x["key"], x["extension"]) for x in planned],
                             [("VoiceOfML/Test\0music/clip.aac", "audio")])
            self.assertEqual([(x["path"], x["extension"]) for x in converted],
                             [("music/clip.aac", "aac")])
            self.assertEqual(selected_records(source, revisions, "media", {"video"}), [])
            self.assertEqual(select_media_records(source, revisions, "video"), [])


class LibraryRebuildPreviewTests(unittest.TestCase):
    repo = 'VoiceOfML/MLMRL-Library'
    other_repo = 'VoiceOfML/Other'
    all_scope = 'all-search-repositories'
    generation = 'a' * 64
    revision = 'b' * 40
    reader_generation = 'c' * 64
    pipeline = 'd' * 40
    digest = 'e' * 64
    root = Path(__file__).resolve().parents[1]

    def row(self, path, *, repo=None, revision=None):
        extension = Path(path).suffix[1:].lower()
        repo = repo or self.repo
        key = f'{repo}\0{path}'
        return {'key': key, 'repo': repo, 'path': path, 'revision': revision or self.revision,
                'extension': extension, 'bytes': 1, 'shard': ebook_shard_for(key, 8),
                'source_sha256': self.digest,
                'old_manifest': f'chapters/ebook/{extension}/{self.digest}/{"f" * 16}/chapter-manifest.json'}

    def plan(self, rows):
        return {'version': 1, 'kind': 'reader-ebook-full-rebuild-plan', 'repo': self.repo,
                'search_generation': self.generation, 'source_revision': self.revision,
                'reader_generation': self.reader_generation, 'shards': 8,
                'max_attempts_per_shard': 200, 'rows': rows, 'remote_writes': 0}

    def all_plan(self, rows, unmapped=()):
        plan = self.plan(rows)
        plan.pop('repo')
        plan.pop('source_revision')
        return dict(plan, scope=self.all_scope, unmapped=list(unmapped),
                    source_revisions={self.repo: self.revision, self.other_repo: '1' * 40,
                                      'VoiceOfML/RepoWithoutEbooks': '2' * 40})

    def unmapped_row(self, path, **options):
        row = self.row(path, **options)
        for field in ('source_sha256', 'old_manifest', 'shard'):
            row.pop(field)
        return dict(row, reason='no_published_chapter_route')

    def run_preview(self, plan, directory, **overrides):
        source = directory / 'rebuild.json'
        source.write_text(json.dumps(plan), encoding='utf-8')
        values = dict(kind='ebook', scope=self.repo, search_generation=self.generation,
                      source_revision=self.revision, reader_generation=self.reader_generation,
                      expected_sha=self.pipeline, rebuild_plan=source, report=directory / 'report.json',
                      output=directory / 'matrix.json', extensions=None, shard_count=8, shard_index=None,
                      retry_failures=False, verify_source_sha=False)
        with patch.object(matrix, 'args', return_value=SimpleNamespace(**{**values, **overrides})), \
                patch.object(matrix, 'HfFileSystem', side_effect=AssertionError('must not read production indexes')), \
                redirect_stdout(io.StringIO()):
            return matrix.main()

    def test_library_plan_keeps_all_pinned_rows_and_publishes_no_worker_matrix(self):
        rows = [self.row(f'目录/Book-{i}.{ext}') for i in range(40)
                for ext in ('epub', 'mobi', 'azw3', 'fb2', 'chm')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.run_preview(self.plan(rows), root), 0)
            self.assertEqual(json.loads((root / 'matrix.json').read_text()), {'include': []})
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['scope'], self.repo)
            self.assertEqual(report['expected_sha'], self.pipeline)
            self.assertEqual(report['search_generation'], self.generation)
            self.assertEqual(report['source_revision'], self.revision)
            self.assertEqual(report['reader_generation'], self.reader_generation)
            self.assertEqual(report['plan_sha256'], hashlib.sha256((root / 'rebuild.json').read_bytes()).hexdigest())
            self.assertEqual((report['shard_count'], report['max_attempts_per_shard'], report['workers']), (8, 200, 4))
            self.assertEqual((report['sources'], report['remote_writes'], report['publication_ready']), (len(rows), 0, False))
            counts = Counter((x['extension'], x['shard']) for x in rows)
            self.assertEqual(report['attempts_by_format_and_shard'],
                             {ext: [counts[ext, shard] for shard in range(8)]
                              for ext in ('epub', 'mobi', 'azw3', 'fb2', 'chm')})

    def test_library_plan_rejects_bad_pins_scope_and_unsafe_rows(self):
        row = self.row('a.epub')
        good = self.plan([row])
        cases = [(good, {field: value}) for field, value in (
            ('scope', None), ('scope', self.repo.lower()), ('kind', 'image'), ('expected_sha', 'short'),
            ('search_generation', ''), ('source_revision', '0' * 40), ('reader_generation', '0' * 64),
            ('extensions', ['epub']), ('shard_count', 4), ('shard_index', 0), ('verify_source_sha', True))]
        cases += [({**good, field: value}, {}) for field, value in (
            ('repo', 'VoiceOfML/Test'), ('expected_sha', '0' * 40), ('remote_writes', 1),
            ('publication_ready', True), ('rows', []), ('rows', [row, row]))]
        cases += [(self.plan([{**row, field: value}]), {}) for field, value in (
            ('key', 'VoiceOfML/Test\0a.epub'), ('repo', 'VoiceOfML/Test'), ('revision', '0' * 40),
            ('shard', (row['shard'] + 1) % 8), ('extension', 'mobi'), ('old_manifest', 'wrong'), ('bytes', 0))]
        for plan, options in cases:
            with self.subTest(plan=plan, options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ValueError):
                    self.run_preview(plan, root, **options)
                self.assertFalse((root / 'matrix.json').exists())
                self.assertFalse((root / 'report.json').exists())

    def test_library_attempt_limit_is_per_real_format_shard_and_never_truncates(self):
        rows = []
        for i in range(5_000):
            path = f'Book-{i}.epub'
            if ebook_shard_for(f'{self.repo}\0{path}', 8) == 3:
                rows.append(self.row(path))
                if len(rows) == 201:
                    break
        self.assertEqual(len(rows), 201)
        with tempfile.TemporaryDirectory() as valid, tempfile.TemporaryDirectory() as invalid:
            source = next(self.row(f'{i}.mobi') for i in range(100)
                          if ebook_shard_for(f'{self.repo}\0{i}.mobi', 8) == 3)
            self.assertEqual(self.run_preview(self.plan([*rows[:200], source]), Path(valid)), 0)
            with self.assertRaisesRegex(ValueError, 'refusing a partial plan'):
                self.run_preview(self.plan(rows), Path(invalid))
            self.assertFalse((Path(invalid) / 'matrix.json').exists())

    def test_all_search_plan_counts_each_repo_format_and_shard_without_scheduling_unmapped(self):
        def rows_for(repo, revision, extension, shard, limit):
            rows = []
            for i in range(5_000):
                row = self.row(f'Book-{i}.{extension}', repo=repo, revision=revision)
                if row['shard'] == shard:
                    rows.append(row)
                    if len(rows) == limit:
                        break
            self.assertEqual(len(rows), limit)
            return rows

        library = rows_for(self.repo, self.revision, 'epub', 3, 201)
        other = rows_for(self.other_repo, '1' * 40, 'epub', 3, 200)
        rows = library[:200] + other + rows_for(self.repo, self.revision, 'mobi', 3, 1) + \
            rows_for(self.repo, self.revision, 'epub', 4, 1)
        unmapped = [self.unmapped_row(library[-1]['path'])]  # same repo/format/shard already at capacity
        self.assertTrue({row['path'] for row in library[:200]} & {row['path'] for row in other})
        options = dict(scope=self.all_scope, source_revision=None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.all_plan(rows, unmapped)
            self.assertEqual(self.run_preview(plan, root, **options), 0)
            self.assertEqual(json.loads((root / 'matrix.json').read_text()), {'include': []})
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['kind'], 'reader-all-search-repositories-ebook-rebuild-preview')
            self.assertEqual(report['scope'], self.all_scope)
            self.assertEqual(report['source_revisions'], plan['source_revisions'])
            self.assertEqual(report['source_heads_changed'], [])
            self.assertNotIn('repo', report)
            self.assertNotIn('source_revision', report)
            self.assertEqual((report['sources'], report['unmapped']), (402, 1))
            self.assertEqual((report['remote_writes'], report['publication_ready']), (0, False))
            counts = Counter((x['repo'], x['extension'], x['shard']) for x in rows)
            self.assertEqual(report['attempts_by_repo_format_and_shard'],
                             {repo: {ext: [counts[repo, ext, shard] for shard in range(8)]
                                     for ext in ('epub', 'mobi', 'azw3', 'fb2', 'chm')}
                              for repo in plan['source_revisions']})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'refusing a partial plan'):
                self.run_preview(self.all_plan([*rows, library[-1]]), root, **options)
            self.assertFalse((root / 'matrix.json').exists())
            self.assertFalse((root / 'report.json').exists())

    def test_all_search_plan_with_only_unmapped_sources_schedules_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.all_plan([], [self.unmapped_row('unmapped.chm')])
            self.assertEqual(self.run_preview(plan, root, scope=self.all_scope, source_revision=None), 0)
            self.assertEqual(json.loads((root / 'matrix.json').read_text()), {'include': []})
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual((report['sources'], report['unmapped']), (0, 1))

    def test_all_search_plan_reports_head_drift_without_enabling_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = dict(self.all_plan([self.row('a.epub')]), source_heads_changed=[self.other_repo])
            self.assertEqual(self.run_preview(plan, root, scope=self.all_scope, source_revision=None), 0)
            self.assertEqual(json.loads((root / 'matrix.json').read_text()), {'include': []})
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['source_heads_changed'], [self.other_repo])
            self.assertFalse(report['publication_ready'])

    def test_all_search_plan_rejects_mixed_schema_wrong_revisions_and_overlap(self):
        row = self.row('a.epub', repo=self.other_repo, revision='1' * 40)
        unmapped = self.unmapped_row('b.chm', repo=self.other_repo, revision='1' * 40)
        good = self.all_plan([row], [unmapped])
        cases = [(good, {'scope': self.all_scope.upper()}), (good, {'source_revision': self.revision}),
                 (self.plan([self.row('a.epub')]), {})]
        cases += [({**good, field: value}, {}) for field, value in (
            ('version', True), ('scope', self.repo), ('repo', None), ('source_revision', None),
            ('source_revisions', {}), ('source_revisions', {self.other_repo: 'not-a-sha'}),
            ('source_revisions', {self.repo: self.revision}), ('unmapped', None),
            ('source_heads_changed', self.repo), ('source_heads_changed', ['VoiceOfML/Unknown']),
            ('source_heads_changed', [self.repo, self.repo]), ('source_heads_changed', [None]),
            ('rows', [{**row, 'revision': self.revision}]), ('rows', [unmapped]),
            ('unmapped', [{**unmapped, 'revision': self.revision}]),
            ('unmapped', [self.unmapped_row('a.epub', repo=self.other_repo, revision='1' * 40)]),
            ('unmapped', [{**unmapped, 'old_manifest': row['old_manifest']}]))]
        for plan, overrides in cases:
            with self.subTest(plan=plan, overrides=overrides), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                options = {'scope': self.all_scope, 'source_revision': None, **overrides}
                with self.assertRaises(ValueError):
                    self.run_preview(plan, root, **options)
                self.assertFalse((root / 'matrix.json').exists())
                self.assertFalse((root / 'report.json').exists())

    def workflow(self):
        return yaml.safe_load((self.root / '.github/workflows/reader-ebook-stream.yml').read_text())

    def test_workflow_preview_gate_is_manual_pinned_and_write_jobs_exclude_any_scope(self):
        jobs = self.workflow()['jobs']
        self.assertEqual(jobs['plan']['permissions'], {'contents': 'read'})
        for name in ('ebook-stream', 'finalize'):
            self.assertIn("inputs.scope == '' &&", jobs[name]['if'])
            self.assertEqual(jobs[name]['concurrency']['group'], 'reader-sidecar')
        gate = next(x['run'] for x in jobs['plan']['steps'] if x.get('name') == 'Validate automation and current main')
        env = dict(os.environ, SCOPE=self.repo, AUTOMATION_ID='', DEFER_UNIFY='false', FINALIZE_ONLY='false',
                   APPLY='false', EXTENSION='all', EVENT_NAME='workflow_dispatch', GITHUB_REPOSITORY='anftm/pipeline',
                   EXPECTED_SHA=self.pipeline, SEARCH_GENERATION=self.generation, GITHUB_SHA=self.pipeline,
                   GITHUB_WORKFLOW_SHA=self.pipeline, LOCAL_SHA=self.pipeline, REMOTE_SHA=self.pipeline,
                   GITHUB_REF='refs/heads/main', STAGE_REPO='', STAGE_EXTENSION='', STAGE_SHARD='',
                   GITHUB_WORKFLOW_REF='anftm/pipeline/.github/workflows/reader-ebook-stream.yml@refs/heads/main')
        stub = 'git() { echo "$LOCAL_SHA"; }; gh() { echo "$REMOTE_SHA"; }; python() { "$TEST_PYTHON" "$@"; };\n'
        env['TEST_PYTHON'] = sys.executable
        cases = [({}, True), ({'SCOPE': self.all_scope}, True)] + [({k: v}, False) for k, v in (
            ('SCOPE', self.repo.lower()), ('SCOPE', self.all_scope.upper()), ('SCOPE', 'all'),
            ('AUTOMATION_ID', '0' * 32), ('DEFER_UNIFY', 'true'),
            ('FINALIZE_ONLY', 'true'), ('APPLY', 'true'), ('EVENT_NAME', 'schedule'),
            ('GITHUB_REPOSITORY', 'fork/pipeline'), ('SEARCH_GENERATION', ''), ('EXPECTED_SHA', ''),
            ('LOCAL_SHA', '0' * 40), ('GITHUB_WORKFLOW_SHA', '0' * 40), ('REMOTE_SHA', '0' * 40),
            ('GITHUB_REF', 'refs/tags/main'))]
        cases += [({**overrides, 'SCOPE': self.all_scope}, False) for overrides, allowed in cases
                  if not allowed and 'SCOPE' not in overrides]
        pilot = dict(STAGE_REPO=self.repo, STAGE_EXTENSION='azw3', STAGE_SHARD='0')
        cases += [(pilot, True), (dict(pilot, SCOPE=self.all_scope, STAGE_REPO='VoiceOfML/Teachers'), True)]
        cases += [(dict(pilot, **overrides), False) for overrides in (
            {'SCOPE': ''}, {'APPLY': 'true'}, {'FINALIZE_ONLY': 'true'}, {'AUTOMATION_ID': '0' * 32},
            {'STAGE_REPO': ''}, {'STAGE_REPO': 'VoiceOfML/Teachers'},
            {'SCOPE': self.all_scope, 'STAGE_REPO': self.other_repo}, {'STAGE_EXTENSION': ''},
            {'STAGE_EXTENSION': 'all'}, {'STAGE_SHARD': ''}, {'STAGE_SHARD': '8'})]
        for overrides, allowed in cases:
            with self.subTest(overrides=overrides):
                result = subprocess.run(['bash', '-e', '-c', stub + gate + '\necho preview-allowed'], cwd=self.root,
                                        env={**env, **overrides}, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                self.assertEqual('preview-allowed' in result.stdout, allowed)

    def test_workflow_passes_pins_to_cli_and_only_hands_off_a_preview_artifact(self):
        steps = self.workflow()['jobs']['plan']['steps']
        preview = next(step for step in steps if step.get('id') == 'rebuild')
        self.assertEqual(preview['env']['SOURCE_REVISION'], '${{ vars.READER_LIBRARY_SOURCE_REVISION }}')
        self.assertEqual(preview['env']['READER_GENERATION'], '${{ vars.READER_LIBRARY_READER_GENERATION }}')
        self.assertEqual(preview['if'], "inputs.scope != ''")
        self.assertNotIn('--apply', preview['run'])
        for scope, label in ((self.repo, 'library'), (self.all_scope, 'all-search-repositories')):
            artifact = next(step for step in steps
                            if step.get('with', {}).get('name') == f'reader-{label}-ebook-rebuild-preview')
            prefix = 'library' if scope == self.repo else 'all-search'
            self.assertEqual(artifact['if'], f"inputs.scope == '{scope}'")
            self.assertEqual(artifact['with']['path'].split(), [f'output/{prefix}-rebuild-plan.json',
                                                               f'output/{prefix}-rebuild-report.json'])
        script = '''git() { echo "$EXPECTED_SHA"; }
gh() { echo "$REMOTE_SHA"; }
python() {
  if [[ "$1 $2" == '-m scripts.reader_ebook_rebuild' ]]; then
    printf '%s\\n' "$@" > cli-args
    cp fixture.json "${!#}"
  elif [[ "$1" == 'scripts/plan_reader_stream_matrix.py' ]]; then
    shift
    "$TEST_PYTHON" -B "$MATRIX_SCRIPT" "$@"
  else
    "$TEST_PYTHON" -B "$@"
  fi
}
''' + preview['run']
        env = dict(os.environ, SCOPE=self.repo, SEARCH_GENERATION=self.generation, SOURCE_REVISION=self.revision,
                   READER_GENERATION=self.reader_generation, EXPECTED_SHA=self.pipeline, EXTENSION='all',
                   LIMIT='200', MAX_SOURCE_BYTES='0', RETRY_FAILURES='false', GITHUB_OUTPUT='github-out',
                   GITHUB_REPOSITORY='anftm/pipeline', REMOTE_SHA=self.pipeline,
                   TEST_PYTHON=sys.executable, MATRIX_SCRIPT=str(Path(matrix.__file__).resolve()))
        invalid_common = [{'LIMIT': '201'}, {'LIMIT': '1'}, {'READER_GENERATION': ''},
                          {'EXTENSION': 'epub'}, {'REMOTE_SHA': '0' * 40}]
        for scope in (self.repo, self.all_scope):
            all_search = scope == self.all_scope
            row = self.row('old.chm')
            plan = self.all_plan([row]) if all_search else self.plan([row])
            cases = [({}, True)] + [(overrides, False) for overrides in invalid_common]
            cases += [({'SOURCE_REVISION': ''}, all_search), ({'SOURCE_REVISION': 'bad'}, all_search)]
            for overrides, allowed in cases:
                with self.subTest(scope=scope, overrides=overrides), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'fixture.json').write_text(json.dumps(plan))
                    run_env = {**env, 'SCOPE': scope, **overrides}
                    if all_search and not overrides:
                        run_env.pop('SOURCE_REVISION')
                    result = subprocess.run(['bash', '-e', '-c', script], cwd=root, env=run_env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    if allowed:
                        scope_args = ['--scope', self.all_scope] if all_search else []
                        source_args = [] if all_search else ['--source-revision', self.revision]
                        prefix = 'all-search' if all_search else 'library'
                        self.assertEqual((root / 'cli-args').read_text().splitlines(),
                                         ['-m', 'scripts.reader_ebook_rebuild', *scope_args,
                                          '--search-generation', self.generation, *source_args,
                                          '--reader-generation', self.reader_generation,
                                          '--output', f'output/{prefix}-rebuild-plan.json'])
                        digest = hashlib.sha256((root / f'output/{prefix}-rebuild-plan.json').read_bytes()).hexdigest()
                        self.assertEqual((root / 'github-out').read_text(), f'matrix={{"include":[]}}\nplan_sha256={digest}\n')
                    else:
                        self.assertEqual((root / 'cli-args').exists(), 'REMOTE_SHA' in overrides)
                        self.assertFalse((root / 'github-out').exists())

    def test_workflow_stage_uses_only_pinned_preview_artifacts_and_no_converter_credentials(self):
        jobs = self.workflow()['jobs']
        stage = jobs['preview-stage']
        self.assertEqual(stage['needs'], 'plan')
        self.assertEqual(stage['permissions'], {'contents': 'read'})
        self.assertIn("inputs.scope != '' && inputs.apply == false", stage['if'])
        self.assertEqual(stage['env']['PLAN_SHA256'], '${{ needs.plan.outputs.rebuild_plan_sha256 }}')
        self.assertEqual(jobs['plan']['outputs']['rebuild_plan_sha256'], '${{ steps.rebuild.outputs.plan_sha256 }}')
        self.assertNotIn('secrets.', json.dumps(stage))
        self.assertNotIn('--apply', json.dumps(stage))
        steps = stage['steps']
        self.assertIs(steps[0]['with']['persist-credentials'], False)
        download = next(s for s in steps if s.get('uses', '').startswith('actions/download-artifact@'))
        self.assertNotIn('run-id', download['with'])  # only this run's plan artifact
        calibre = next(s['run'] for s in steps if s.get('name', '').startswith('Install pinned Ubuntu Calibre'))
        self.assertIn('calibre=7.6.0+ds-1build1 calibre-bin=7.6.0+ds-1build1', calibre)
        self.assertIn('packages+=(p7zip-full)', calibre)
        self.assertIn('ebook-convert --version', calibre)
        kindle = next(s['run'] for s in steps if s.get('name', '').startswith('Fetch audited KindleUnpack'))
        self.assertIn('https://github.com/kevinhendricks/KindleUnpack.git "$revision"', kindle)
        self.assertIn('revision=bf0ca6ece4e73494625e7950be3e259b6260774c', kindle)
        self.assertEqual(steps[-1]['with']['path'], '${{ runner.temp }}/ebook-candidates/')
        self.assertEqual(steps[-1]['with']['name'], 'reader-ebook-rebuild-candidate-preview')
        run = next(s['run'] for s in steps if s.get('name') == 'Build local candidates only')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'python'
            python.write_text(f'#!/bin/bash\nset -eu\n[[ -z "${{HF_TOKEN+x}}${{GH_TOKEN+x}}${{HF_S3_SECRET_ACCESS_KEY+x}}" ]]\n'
                              f'printf "%s\\n" "$@" > "{root / "args"}"\n')
            python.chmod(0o700)
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}', RUNNER_TEMP=directory,
                       PLAN_PREFIX='all-search', PLAN_SHA256=self.digest, STAGE_REPO=self.repo,
                       STAGE_EXTENSION='mobi', STAGE_SHARD='0', HF_TOKEN='not-for-converter',
                       GH_TOKEN='also-not-for-converter', HF_S3_SECRET_ACCESS_KEY='no-bucket-access')
            subprocess.run(['bash', '-e', '-c', run], env=env, check=True, timeout=5)
            self.assertEqual((root / 'args').read_text().splitlines(), [
                '-m', 'scripts.reader_ebook_rebuild_stage', '--plan', str(root / 'ebook-plan/all-search-rebuild-plan.json'),
                '--plan-sha256', self.digest, '--repo', self.repo, '--extension', 'mobi', '--shard', '0',
                '--output', str(root / 'ebook-candidates'), '--kindle-unpacker', str(root / 'KindleUnpack/lib/kindleunpack.py')])


if __name__ == "__main__":
    unittest.main()
