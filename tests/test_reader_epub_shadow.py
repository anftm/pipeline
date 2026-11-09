"""EPUB content shadows are bounded and never write remote Reader roots."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import reader_epub_shadow as subject
from scripts import reader_epub_plan, reader_stream_index, search_snapshot

REPO, REV = 'VoiceOfML/Test', 'a' * 40


def epub():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        archive.writestr('content.opf', '<package><manifest><item id="a" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/></spine></package>')
        archive.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Updated</title></head><body><p>New content</p></body></html>')
    return output.getvalue()


def plan(data, *, proof='current-lfs-sha256'):
    key = REPO + '\0book.epub'
    row = {'key': key, 'repo': REPO, 'path': 'book.epub', 'source_revision': REV,
           'reason': 'content_changed', 'proof': proof, 'previous_revision': 'b' * 40,
           'previous_source_sha256': 'c' * 64, 'previous_manifest': 'chapters/ebook/epub/old/chapter-manifest.json',
           'current_source_sha256': hashlib.sha256(data).hexdigest(), 'previous_git_blob': None, 'current_git_blob': None}
    if proof == 'exact-revision-git-blobs':
        row['previous_git_blob'] = 'd' * 40
        row['current_git_blob'] = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
        row['current_source_sha256'] = None
    body = {'version': 1, 'kind': 'reader-epub-exact-build-plan', 'operation': 'f' * 32,
            'shard': reader_stream_index.shard_for(key, 8), 'search_generation': '1' * 64,
            'reader_generation': '2' * 64, 'canonical_index_sha256': '3' * 64, 'shard_index_sha256': '4' * 64,
            'source_revisions_sha256': '5' * 64, 'profile': reader_epub_plan.builder.EPUB_PROFILE,
            'max_attempts': 3, 'sources': [row], 'remote_writes': 0, 'publication_ready': False,
            'external_writers_verified': False}
    body['plan_sha256'] = hashlib.sha256(search_snapshot.canonical(body)).hexdigest()
    return body


class ReaderEpubShadowTests(unittest.TestCase):
    def test_real_epub_conversion_verifies_pinned_lfs_and_retains_old_manifest(self):
        source = epub(); expected = plan(source)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'shadow'
            def fetch(_url, target, _token): target.write_bytes(source)
            report = subject.build(expected, output, token='test-only', fetch=fetch)
            row = report['sources'][0]
            self.assertEqual(row['source']['sha256'], hashlib.sha256(source).hexdigest())
            self.assertEqual(row['previous_manifest'], expected['sources'][0]['previous_manifest'])
            self.assertGreaterEqual(row['chapter_count'], 1)
            self.assertTrue(any(item['path'] == 'chapter-manifest.json' for item in row['files']))
            self.assertTrue(any(item['path'] == 'epub-search-index.json.gz' for item in row['files']))
            self.assertTrue((output / '0' / 'source.epub').is_file())
            self.assertEqual(report['remote_writes'], 0)
            self.assertFalse(report['publication_ready'])
            with self.assertRaisesRegex(ValueError, 'already exists'):
                subject.build(expected, output, fetch=fetch)

    def test_stage_preview_binds_protection_and_every_file_without_writing(self):
        source = epub(); expected = plan(source)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'shadow'
            def fetch(_url, target, _token): target.write_bytes(source)
            report = subject.build(expected, output, fetch=fetch)
            staged = subject.staging_plan(expected, report, output)
            self.assertEqual(staged['remote_writes'], 0)
            self.assertFalse(staged['index_written'])
            self.assertFalse(staged['publication_ready'])
            action = staged['actions'][0]
            self.assertEqual(action['previous_manifest'], expected['sources'][0]['previous_manifest'])
            self.assertTrue(action['protection_path'].startswith('reader-index/processing/epub-exact-'))
            self.assertTrue(all(row['remote'].startswith(action['root'] + '/') for row in action['files']))
            self.assertGreater(len(action['files']), 1)
            changed = output / '0' / 'bundle' / 'chapter-manifest.json'
            changed.write_bytes(changed.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'changed after verification'):
                subject.staging_plan(expected, report, output)

    def test_stage_preview_rejects_plan_or_source_drift(self):
        source = epub(); expected = plan(source)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'shadow'
            def fetch(_url, target, _token): target.write_bytes(source)
            report = subject.build(expected, output, fetch=fetch)
            report['operation'] = 'a' * 32
            with self.assertRaisesRegex(ValueError, 'differs from exact plan'):
                subject.staging_plan(expected, report, output)
            report['operation'] = expected['operation']
            (output / '0' / 'source.epub').write_bytes(source + b'changed')
            with self.assertRaisesRegex(ValueError, 'differs'):
                subject.staging_plan(expected, report, output)

    def test_git_blob_or_lfs_mismatch_rejects_before_parser(self):
        source = epub()
        for proof in ('current-lfs-sha256', 'exact-revision-git-blobs'):
            value = plan(source, proof=proof)
            with tempfile.TemporaryDirectory() as directory, patch.object(subject, 'run_parser') as parser:
                def fetch(_url, target, _token): target.write_bytes(source + b'changed')
                with self.subTest(proof=proof), self.assertRaisesRegex(ValueError, 'differs'):
                    subject.build(value, Path(directory) / 'output', fetch=fetch, parse=parser)
                parser.assert_not_called()

    def test_git_blob_verification_accepts_exact_source(self):
        source = epub(); row = plan(source, proof='exact-revision-git-blobs')['sources'][0]
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'book.epub';p.write_bytes(source)
            self.assertEqual(subject.source_identity(p, row)['git_blob'], row['current_git_blob'])
            row['previous_source_sha256'] = hashlib.sha256(source).hexdigest()
            with self.assertRaisesRegex(ValueError, 'unchanged'):
                subject.source_identity(p, row)

    def test_zip_symlinks_duplicates_encryption_or_limits_fail_before_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / 'duplicate.epub'
            with zipfile.ZipFile(duplicate, 'w') as archive:
                archive.writestr('same', 'a');archive.writestr('same', 'b')
            with self.assertRaisesRegex(ValueError, 'duplicate'): subject.validate_archive(duplicate)
            symlink = root / 'link.epub'
            with zipfile.ZipFile(symlink, 'w') as archive:
                info = zipfile.ZipInfo('link');info.create_system = 3;info.external_attr = (0o120777 << 16)
                archive.writestr(info, 'target')
            with self.assertRaisesRegex(ValueError, 'symbolic link'): subject.validate_archive(symlink)
            with patch.object(subject, 'MAX_ARCHIVE_MEMBERS', 1):
                normal = root / 'normal.epub';normal.write_bytes(epub())
                with self.assertRaisesRegex(ValueError, 'entry count'): subject.validate_archive(normal)

    def test_parser_runs_with_minimal_nonsecret_environment(self):
        from unittest.mock import Mock
        with patch.dict('os.environ', {'HF_TOKEN': 'do-not-pass', 'GITHUB_TOKEN': 'do-not-pass', 'PYTHONPATH': '/secret'}, clear=False),                 patch.object(subject.subprocess, 'run') as run:
            subject.run_parser(Path('/tmp/book.epub'), Path('/tmp/bundle'))
        env = run.call_args.kwargs['env']
        self.assertNotIn('HF_TOKEN', env)
        self.assertNotIn('GITHUB_TOKEN', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertEqual(run.call_args.kwargs['timeout'], 600)

    def test_workflow_is_manual_and_only_uploads_the_report(self):
        import yaml
        flow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/reader-epub-shadow.yml').read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        steps = flow['jobs']['shadow']['steps']
        artifact = next(x for x in steps if x.get('uses') == 'actions/upload-artifact@v7')
        self.assertEqual(artifact['with']['path'].splitlines(),
                         ['output/epub-shadow/report.json', 'output/epub-shadow/staging-preview.json'])
        commands = '\n'.join(x.get('run', '') for x in steps)
        for forbidden in ('--apply', 'git push', 'workflow run', 'delete'):
            self.assertNotIn(forbidden, commands)

    def test_plan_rejects_extra_fields_and_unbound_content_proof(self):
        import copy
        for change in ('extra-plan', 'extra-source', 'missing-sha', 'same-sha', 'unknown-proof'):
            value=copy.deepcopy(plan(epub()))
            if change=='extra-plan':value['credential']='must-not-be-in-plan'
            elif change=='extra-source':value['sources'][0]['credential']='must-not-be-in-plan'
            elif change=='missing-sha':value['sources'][0]['current_source_sha256']=None
            elif change=='same-sha':value['sources'][0]['current_source_sha256']=value['sources'][0]['previous_source_sha256']
            else:value['sources'][0]['proof']='unknown'
            value['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in value.items() if k!='plan_sha256'})).hexdigest()
            with self.subTest(change=change),self.assertRaises(ValueError):subject._verify_plan(value)

    def test_one_source_plan_requires_matching_bound(self):
        value=plan(epub());value['max_attempts']=1
        value['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in value.items() if k!='plan_sha256'})).hexdigest()
        self.assertIs(subject._verify_plan(value),value)
        value['max_attempts']=2
        value['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in value.items() if k!='plan_sha256'})).hexdigest()
        with self.assertRaises(ValueError):subject._verify_plan(value)

    def test_redirect_host_is_restricted(self):
        subject._check_url('https://huggingface.co/datasets/VoiceOfML/Test/resolve/' + REV + '/book.epub')
        for url in ('http://huggingface.co/a', 'https://evil.example/a', 'https://u:p@huggingface.co/a'):
            with self.subTest(url=url), self.assertRaises(ValueError): subject._check_url(url)


if __name__ == '__main__': unittest.main()
