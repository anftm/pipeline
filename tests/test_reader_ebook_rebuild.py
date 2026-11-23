import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

from scripts import reader_ebook_rebuild as rebuild, reader_ebook_rebuild_stage as stage


class ScopedEbookRebuildPlanTests(unittest.TestCase):
    revision = '1' * 40

    def source(self, name, ext='epub', repo=rebuild.REPO):
        key = repo + '\0' + name
        return key, dict(key=key, repo=repo, path=name, extension=ext, revision=self.revision, bytes=10)

    @staticmethod
    def route(ext='epub'):
        return dict(s=2, m='e', b=rebuild.TARGET_BUCKET,
                    p=f'chapters/ebook/{ext}/' + 'a' * 64 + '/' + 'b' * 16 + '/chapter-manifest.json')

    def test_rebuild_all_current_library_chapters_without_other_datasets_or_formats(self):
        sources = dict([self.source('x.epub'), self.source('a.mobi', 'mobi'),
                        self.source('b.chm', 'chm'), self.source('ignore.pdf', 'pdf'),
                        self.source('not-scoped.epub', repo='VoiceOfML/MLMRL-Hub')])
        routes = {key: self.route(source['extension']) for key, source in sources.items()}
        rows = rebuild.planned_rows(sources, routes, revision=self.revision)
        self.assertEqual(Counter(row['extension'] for row in rows), {'epub': 1, 'chm': 1, 'mobi': 1})
        self.assertTrue(all(row['source_sha256'] == 'a' * 64 and row['repo'] == rebuild.REPO for row in rows))
        self.assertTrue(all(0 <= row['shard'] < 8 for row in rows))

    def test_does_not_silently_omit_unmapped_changed_or_over_limit_items(self):
        sources = dict([self.source('x.epub'), self.source('y.epub')]); routes = {k: self.route() for k in sources}
        with self.assertRaisesRegex(ValueError, 'revision|pinned'):
            rebuild.planned_rows(sources, routes, revision='2' * 40)
        with self.assertRaisesRegex(ValueError, 'no published chapter route'):
            rebuild.planned_rows(sources, {}, revision=self.revision)
        with patch.object(rebuild, 'shard_for', return_value=3), patch.object(rebuild, 'MAX_PER_SHARD', 1):
            with self.assertRaisesRegex(ValueError, 'refusing a partial census'):
                rebuild.planned_rows(sources, routes, revision=self.revision)

    def test_all_repositories_keep_repo_shards_isolated_and_list_unmapped(self):
        other = 'VoiceOfML/MLMRL-Hub'
        sources = dict([self.source('same.epub'), self.source('same.epub', repo=other), self.source('no.chm', 'chm')])
        revisions = {rebuild.REPO: self.revision, other: '2'*40}
        sources[other+'\0same.epub']['revision'] = revisions[other]
        routes = {key: self.route() for key in sources if key.endswith('.epub')}
        with patch.object(rebuild, 'shard_for', return_value=3), patch.object(rebuild, 'MAX_PER_SHARD', 1):
            rows, unmapped = rebuild._rows(sources, routes, revisions=revisions, allow_unmapped=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual([x['path'] for x in unmapped], ['no.chm'])
        for row in rows:
            row['shard'] = rebuild.shard_for(row['key'], rebuild.SHARDS)
        value = dict(kind='reader-ebook-full-rebuild-plan', version=1, scope=rebuild.ALL_SCOPE,
                     source_revisions=revisions, rows=rows, shards=8, remote_writes=0,
                     search_generation='a'*64, reader_generation='b'*64, unmapped=unmapped)
        selected = next(row for row in rows if row['repo'] == other)
        with self.assertRaisesRegex(ValueError, 'exact repository shard'):
            stage.shard_rows(value, 'epub', selected['shard'])
        self.assertEqual(stage.shard_rows(value, 'epub', selected['shard'], repo=other), [selected])
        selected['revision'] = revisions[rebuild.REPO]
        with self.assertRaisesRegex(ValueError, 'unexpected source'):
            stage.shard_rows(value, 'epub', selected['shard'], repo=other)

    def test_rebuild_chm_uses_exact_chapter_reference_not_native_epub(self):
        key, source = self.source('book.chm', 'chm')
        route = self.route('chm'); old = route['p']
        route.update(c=old, cb=rebuild.TARGET_BUCKET, p='native/ebook/chm/document.epub')
        self.assertEqual(rebuild.planned_rows({key: source}, {key: route}, revision=self.revision)[0]['old_manifest'], old)

    def test_stage_builds_all_and_keeps_prior_route_for_held_book(self):
        source = self.source('works.epub')
        shard = rebuild.shard_for(source[0], rebuild.SHARDS)
        failed = next(self.source(f'failure-{n}.epub') for n in range(100)
                      if rebuild.shard_for(self.source(f'failure-{n}.epub')[0], rebuild.SHARDS) == shard)
        records = dict([source, failed]); routes = {k: self.route() for k in records}
        rows = rebuild.planned_rows(records, routes, revision=self.revision)
        raw = json.dumps(dict(kind='reader-ebook-full-rebuild-plan', version=1, repo=rebuild.REPO,
                              shards=8, remote_writes=0, source_revision=self.revision,
                              search_generation='a'*64, reader_generation='b'*64, rows=rows)).encode()
        digest = hashlib.sha256(raw).hexdigest()
        def build(item, dest, token, bucket):
            if item['path'].startswith('failure'):
                raise RuntimeError('unusable source')
            bundle = dest/'bundle'; bundle.mkdir(parents=True)
            (bundle/'chapter-manifest.json').write_text(json.dumps({
                'kind': 'epub-chapters', 'version': 1, 'chapters': [
                    {'index': 1, 'path': 'chapters/chapter-0001.xhtml', 'bytes': 10, 'sha256': 'c'*64}],
            }))
            return dict(key=source[0], source_sha256='a'*64, source_revision=self.revision, source_bytes=10), {}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'new'
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                stage.stage(raw, '0'*64, 'epub', shard, out, build=build)
            report = stage.stage(raw, digest, 'epub', shard, out, build=build)
            self.assertEqual((report['attempted'], report['candidates'], report['held']), (2, 1, 1))
            self.assertEqual(report['remote_writes'], 0)
            self.assertFalse(report['publication_ready'])
            results = [json.loads(line) for line in (out/'results.jsonl').read_text().splitlines()]
            self.assertEqual({r['old_manifest'] for r in results}, {self.route()['p']})
            self.assertEqual(len(list((out/'bundles').iterdir())), 1)
            tiny = stage.stage(raw, digest, 'epub', shard, Path(tmp)/'tiny', build=build, max_retained_bytes=1)
            self.assertEqual((tiny['candidates'], tiny['held']), (0, 2))
