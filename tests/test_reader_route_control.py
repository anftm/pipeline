import copy
import gzip
import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from scripts import publish_reader_v3 as publication, reader_route_control as control
from scripts import publish_search_reader_index as pages_publication
from tests.test_reader_route_plan import setup_store, ASSETS, KEY, OTHER


def projection(store):
    pointer, catalog = publication.current(store)
    return pointer, catalog, publication.project_sidecar(publication.pinned_base(store, catalog, {}), catalog)


class FakeResponse:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass
    def raise_for_status(self):
        pass
    def iter_bytes(self):
        yield self.body


class Consumers:
    def __init__(self, store, *, stale=None, missing=None):
        self.store, self.stale, self.missing = store, stale, missing
    def stream(self, method, url, **_kwargs):
        self.assert_method(method)
        if self.missing and self.missing in url:
            raise OSError('site unavailable')
        pointer, catalog, sidecar = projection(self.store)
        if self.stale and self.stale in url:
            sidecar['f'][KEY] = {'s': 2, 'm': 'i', 'p': 'old', 'b': ASSETS}
        if '/api/reader-v3-status' in url:
            return FakeResponse(publication.encode({'version': 1, 'surface': 'hf', 'active': True,
                'generation': pointer['generation'], 'catalog_sha256': pointer['catalog']['sha256'],
                'files': len(catalog['files'])}))
        if '/api/reader-assets' in url:
            sidecar['f'] = {key: ({'s': 4} if value.get('s') == 4 else value)
                            for key, value in sidecar['f'].items()}
            return FakeResponse(publication.encode(sidecar))
        packed = gzip.compress(publication.encode(sidecar), mtime=0)
        if 'reader_v3_receipt.json' in url:
            return FakeResponse(publication.encode({'version': 1, 'surface': 'pages', 'active': True,
                'generation': pointer['generation'], 'catalog_sha256': pointer['catalog']['sha256'],
                'files': len(catalog['files']), 'reader_contract': 'pdf-reading-v3-v1',
                'sidecar_sha256': hashlib.sha256(packed).hexdigest()}))
        if 'reader_assets.json.gz' in url:
            return FakeResponse(packed)
        raise AssertionError(url)
    @staticmethod
    def assert_method(method):
        if method != 'GET':
            raise AssertionError(method)


class RouteControlTests(unittest.TestCase):
    def setUp(self):
        self.store, self.base = setup_store(override=True)
        self.options = {'repo': 'VoiceOfML/Test', 'path': '目录/一.pdf'}
        self.pointer = publication.current(self.store)[0]

    def remove(self, *, apply=True):
        return control.change(self.store, self.options, self.pointer['generation'], action='remove', apply=apply)

    def test_preview_and_apply_remove_exact_route_but_no_object_or_search_data(self):
        original = copy.deepcopy(self.store.objects)
        report = self.remove(apply=False)
        self.assertFalse(report['applied'])
        self.assertEqual(self.store.objects, original)
        result = self.remove()
        pointer, catalog, current = projection(self.store)
        self.assertTrue(result['applied'])
        self.assertEqual(result['generation'], pointer['generation'])
        self.assertEqual(catalog['suppressed_sources'], [KEY])
        self.assertNotIn(KEY, catalog['files'])
        self.assertNotIn(KEY, current['f'])
        self.assertEqual(current['f'][OTHER], self.base['f'][OTHER])
        self.assertFalse(result['objects_deleted'])
        self.assertFalse(result['search_record_removed'])
        self.assertTrue(original.keys() <= self.store.objects.keys())
        self.assertEqual(self.store.objects[ASSETS, 'reader-index/reader_assets.json.gz'],
                         original[ASSETS, 'reader-index/reader_assets.json.gz'])

    def test_rebuild_and_unify_cannot_reintroduce_route(self):
        self.remove()
        updated = copy.deepcopy(self.base)
        updated['f']['VoiceOfML/Test\0new.txt'] = {'s': 4, 'om': 'ignored diagnostic'}
        self.store.objects[ASSETS, 'reader-index/reader_assets.json.gz'] = gzip.compress(publication.encode(updated), mtime=0)
        publication.unify(self.store, apply=True)
        pointer, catalog, current = projection(self.store)
        self.assertEqual(catalog['suppressed_sources'], [KEY])
        self.assertNotIn(KEY, current['f'])
        self.assertIn('VoiceOfML/Test\0new.txt', current['f'])
        self.assertTrue(publication.unify(self.store, apply=True)['unchanged'])
        verified = control.verify_consumers(self.store, Consumers(self.store), self.options, pointer['generation'])
        self.assertTrue(verified['complete'])
        self.assertEqual(set(verified['consumers']), {'hf', 'pages'})

    def test_two_suppressions_remain_independent_and_each_can_be_restored(self):
        first = self.remove()
        options = {'repo': 'VoiceOfML/Test', 'path': '目录/二.pdf'}
        second = control.change(self.store, options, first['generation'], action='remove', apply=True)
        _, catalog, current = projection(self.store)
        self.assertEqual(catalog[control.FIELD], sorted([KEY, OTHER]))
        self.assertNotIn(KEY, current['f']); self.assertNotIn(OTHER, current['f'])
        restored = control.change(self.store, self.options, second['generation'], action='restore', apply=True)
        _, catalog, current = projection(self.store)
        self.assertEqual(catalog[control.FIELD], [OTHER])
        self.assertIn(KEY, current['f']); self.assertNotIn(OTHER, current['f'])
        self.assertEqual(restored['generation'], publication.current(self.store)[0]['generation'])

    def test_restore_uses_current_canonical_and_never_restores_old_override(self):
        result = self.remove()
        restored = control.change(self.store, self.options, result['generation'], action='restore', apply=True)
        pointer, catalog, current = projection(self.store)
        self.assertEqual(restored['generation'], pointer['generation'])
        self.assertEqual(catalog['suppressed_sources'], [])
        self.assertEqual(current['f'][KEY], self.base['f'][KEY])
        self.assertNotIn(KEY, catalog['files'])
        self.assertEqual(current['f'][OTHER], self.base['f'][OTHER])
        verified = control.verify_consumers(self.store, Consumers(self.store), self.options,
                                            restored['generation'], action='restore')
        self.assertEqual(verified['status'], 'verified-restored')
        self.store.writes.clear()
        retry = control.change(self.store, self.options, restored['generation'], action='restore', apply=True)
        self.assertTrue(retry['unchanged'])
        self.assertEqual(retry['generation'], restored['generation'])
        self.assertEqual(self.store.writes, [])

    def test_stale_parent_invalid_options_and_missing_route_do_not_write(self):
        for options, generation in [(self.options, 'f' * 64), ({'repo': 'VoiceOfML/Test', 'path': '../x'}, self.pointer['generation']),
                                    ({'repo': 'VoiceOfML/Test', 'path': 'missing'}, self.pointer['generation'])]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                control.change(self.store, options, generation, action='remove', apply=True)
        self.assertEqual(self.store.writes, [])

    def test_interrupted_pointer_write_is_retryable_and_old_projection_remains_intact(self):
        self.store.fail_pointer = True
        with self.assertRaises(OSError):
            self.remove()
        self.assertEqual(publication.current(self.store)[0], self.pointer)
        self.store.fail_pointer = False
        result = self.remove()
        self.assertNotIn(KEY, projection(self.store)[2]['f'])
        self.assertEqual(result['generation'], publication.current(self.store)[0]['generation'])

    def test_changed_canonical_or_pointer_prevents_write(self):
        read = self.store.read_bytes
        first = read(ASSETS, 'reader-index/reader_assets.json.gz')
        changed = gzip.compress(publication.encode({'v': 1, 'f': {}}), mtime=0)
        n = 0
        def drifting(bucket, path):
            nonlocal n
            if (bucket, path) == (ASSETS, 'reader-index/reader_assets.json.gz'):
                n += 1
                return first if n == 1 else changed
            return read(bucket, path)
        with patch.object(self.store, 'read_bytes', side_effect=drifting), self.assertRaisesRegex(ValueError, 'changed'):
            self.remove()
        self.assertEqual(self.store.writes, [])

    def test_retry_after_success_uses_current_generation_and_is_idempotent(self):
        result = self.remove()
        self.store.writes.clear()
        again = control.change(self.store, self.options, result['generation'], action='remove', apply=True)
        self.assertTrue(again['unchanged'])
        self.assertEqual(again['generation'], result['generation'])
        self.assertEqual(self.store.writes, [])

    def test_unauthorized_writer_and_invalid_suppression_are_rejected(self):
        self.store.allowed = False
        with self.assertRaisesRegex(RuntimeError, 'serialized'):
            self.remove()
        self.assertEqual(self.store.writes, [])
        self.store.allowed = True
        pointer, catalog = publication.current(self.store)
        for keys in ([KEY, KEY], [KEY, 'bad'], [OTHER, KEY], ['../repo\0a']):
            bad = {**catalog, control.FIELD: keys, 'parent_generation': pointer['generation']}
            bad.pop('generation')
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                publication.commit_catalog(self.store, bad, pointer, apply=False)
        self.assertEqual(self.store.writes, [])

    def test_old_generation_or_missing_sites_never_report_success(self):
        result = self.remove()
        generation = result['generation']
        with self.assertRaisesRegex(ValueError, 'changed'):
            control.verify_consumers(self.store, Consumers(self.store), self.options, self.pointer['generation'])
        for stale in ('voiceofml-search', 'vomebook.github.io'):
            with self.subTest(stale=stale), self.assertRaisesRegex(RuntimeError, 'incomplete'):
                control.verify_consumers(self.store, Consumers(self.store, stale=stale), self.options, generation)
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            control.verify_consumers(self.store, Consumers(self.store, missing='vomebook.github.io'), self.options, generation)

    def test_lagging_site_is_retried_without_gc_ack_writes(self):
        result = self.remove()
        self.store.writes.clear()
        client = Consumers(self.store, stale='vomebook.github.io')
        now = [0]
        def advance(seconds):
            now[0] += seconds
            client.stale = None
        verified = control.verify_consumers(self.store, client, self.options, result['generation'],
                                            timeout_seconds=60, clock=lambda: now[0], sleep_fn=advance)
        self.assertTrue(verified['complete'])
        self.assertEqual(now[0], 20)
        self.assertEqual(self.store.writes, [])

    def test_new_generation_during_propagation_fails_without_mixing_receipts(self):
        result = self.remove()
        client = Consumers(self.store, stale='vomebook.github.io')
        clock = [0]
        def advance(seconds):
            clock[0] += seconds
            updated = copy.deepcopy(self.base)
            updated['f']['new-key'] = {'s': 4}
            self.store.objects[ASSETS, 'reader-index/reader_assets.json.gz'] = gzip.compress(publication.encode(updated), mtime=0)
            publication.unify(self.store, apply=True)
        with self.assertRaisesRegex(ValueError, 'changed during verification'):
            control.verify_consumers(self.store, client, self.options, result['generation'],
                                     timeout_seconds=60, clock=lambda: clock[0], sleep_fn=advance)

    def test_other_publication_cannot_drop_suppression_or_reintroduce_override(self):
        self.remove()
        pointer, catalog = publication.current(self.store)
        bad = {k: copy.deepcopy(v) for k, v in catalog.items() if k not in ('generation', control.FIELD)}
        bad['parent_generation'] = pointer['generation']
        with self.assertRaisesRegex(ValueError, 'preserve'):
            publication.commit_catalog(self.store, bad, pointer, apply=True)
        bad[control.FIELD] = []
        bad.pop('generation')
        with self.assertRaisesRegex(ValueError, 'preserve'):
            publication.commit_catalog(self.store, bad, pointer, apply=True)
        pointer, catalog = publication.current(self.store)
        self.assertIn(KEY, catalog[control.FIELD])
        self.assertNotIn(KEY, projection(self.store)[2]['f'])

    def test_commit_rejects_a_pinned_snapshot_that_exposes_a_suppressed_key(self):
        self.remove()
        pointer, catalog = publication.current(self.store)
        canonical = self.store.objects[ASSETS, 'reader-index/reader_assets.json.gz']
        ref = publication.metadata(ASSETS, f'reader-index/snapshots/{hashlib.sha256(canonical).hexdigest()}/reader_assets.json.gz', canonical)
        self.store.objects[ASSETS, ref['path']] = canonical
        bad = {**catalog, 'parent_generation': pointer['generation'], 'base_sidecar': ref}
        bad.pop('generation')
        before = list(self.store.writes)
        with self.assertRaisesRegex(ValueError, 'exposes suppressed'):
            publication.commit_catalog(self.store, bad, pointer, apply=True)
        self.assertEqual(self.store.writes, before)
        self.assertEqual(publication.current(self.store)[0], pointer)

    def test_unrelated_v3_override_withdrawal_preserves_suppression(self):
        self.remove()
        pointer, catalog = publication.current(self.store)
        other_key = 'VoiceOfML/Test\0other.pdf'
        source = 'f' * 64
        ref = publication.metadata('vomebook/pdf-pages-v2',
            f"objects/ff/{source}/{'c' * 16}/reading-manifest.json", b'manifest')
        next_catalog = {**catalog, 'parent_generation': pointer['generation'],
                        'files': {other_key: {'source_sha256': source, 'reading_generation': 'd' * 64,
                                              'resource': ref}}}
        next_catalog.pop('generation')
        publication.commit_catalog(self.store, next_catalog, pointer, apply=True)
        pointer, _ = publication.current(self.store)
        with patch.dict('os.environ', {'GITHUB_ACTOR': 'test'}):
            publication.withdraw(self.store, {'source_key': other_key, 'reading_generation': 'd' * 64},
                                 pointer['generation'], apply=True)
        _, catalog, current = projection(self.store)
        self.assertEqual(catalog[control.FIELD], [KEY])
        self.assertNotIn(KEY, current['f'])

    def test_pages_git_publication_uses_filtered_pinned_generation_and_keeps_other_files(self):
        result = self.remove()
        pointer, catalog, projected = projection(self.store)
        data = gzip.compress(publication.encode(projected), mtime=0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, seed, output = root / 'remote.git', root / 'seed', root / 'reader.gz'
            def git(*args, cwd=None):
                return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)
            git('init', '--bare', '--initial-branch=main', str(remote))
            git('init', '--initial-branch=main', str(seed))
            git('config', 'user.name', 'Fixture', cwd=seed)
            git('config', 'user.email', 'fixture@example.test', cwd=seed)
            (seed / 'data').mkdir()
            (seed / 'data' / 'other.txt').write_text('unchanged')
            git('add', '.', cwd=seed); git('commit', '-m', 'initial', cwd=seed)
            git('remote', 'add', 'origin', str(remote), cwd=seed); git('push', '-u', 'origin', 'main', cwd=seed)
            output.write_bytes(data)
            real_run = pages_publication.run
            def local_run(args, cwd=None, env=None):
                if args[:4] == ['git', 'clone', '--depth', '1']:
                    args = ['git', 'clone', '--depth', '1', str(remote), args[-1]]
                return real_run(args, cwd=cwd, env=env)
            with patch.object(pages_publication, 'run', side_effect=local_run):
                pages_publication.publish_to_pages(output, 'fixture/site', 'dummy', dry_run=False)
            deployed = gzip.decompress(subprocess.check_output(
                ['git', '--git-dir', str(remote), 'show', 'main:data/reader_assets.json.gz']))
            actual = json.loads(deployed)
            self.assertNotIn(KEY, actual['f'])
            self.assertEqual(actual['f'][OTHER], self.base['f'][OTHER])
            self.assertEqual(actual['v3']['generation'], result['generation'])
            self.assertEqual(git('--git-dir', str(remote), 'show', 'main:data/other.txt').stdout, 'unchanged')
            self.assertEqual(publication.current(self.store)[0], pointer)

    def test_existing_withdraw_keeps_suppression(self):
        self.remove()
        pointer, catalog = publication.current(self.store)
        with patch.dict(os.environ, {'GITHUB_ACTOR': 'test'}):
            result = publication.withdraw(self.store, {'source_key': 'other', 'reading_generation': 'e' * 64},
                                          pointer['generation'], apply=True)
        self.assertTrue(result['unchanged'])
        self.assertEqual(publication.current(self.store)[1][control.FIELD], [KEY])


if __name__ == '__main__':
    unittest.main()
