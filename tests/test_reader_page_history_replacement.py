import copy
import gzip
import json
import unittest
from unittest.mock import patch

from scripts import publish_reader_v3 as publication, reader_page_audit as audit
from scripts import reader_page_history_replacement as replacement
from scripts import reader_page_redirects as redirects, reader_page_redirect_retirement as finalization
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW
from tests.test_reader_page_atomic import AtomicStore
from tests.test_reader_page_migrate import MemoryApi


class HistoryReplacementTests(unittest.TestCase):
    def setUp(self):
        index, derived, objects, manifests, self.base = fixture()
        index['files'][0]['new_path'] = NEW
        index['files'][0]['manifest_path'] = NEW.rsplit('/', 1)[0] + '/derived-manifest.json'
        self.store = AtomicStore()
        self.store.api = MemoryApi(derived, objects)
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode({'v': 1, 'f': {}}), mtime=0)
        for path, raw in manifests.items():
            self.store.objects[audit.BUCKET, path] = raw
        self.paths = []

    def history(self, base=None, **fields):
        base = copy.deepcopy(self.base if base is None else base)
        packed = gzip.compress(publication.encode(base), mtime=0)
        ref = publication.metadata(audit.ASSETS,
            f'reader-index/snapshots/{finalization._sha(packed)}/reader_assets.json.gz', packed)
        catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                   'base_sidecar': ref, 'parent_generation': None, **fields}
        catalog['generation'] = publication.text.digest(catalog)
        raw = publication.encode(catalog)
        catalog_ref = publication.metadata(audit.ASSETS,
            f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw)
        path = replacement.PROMOTIONS + catalog['generation'] + '.json'
        promotion = {'version': 1, 'kind': 'reader-reading-promotion',
                     'generation': catalog['generation'], 'resources': [catalog_ref],
                     'acks': {'hf': False, 'pages': False}, 'retention_days': 30,
                     'created_at': '2099-01-01T00:00:00+00:00'}
        self.store.objects[audit.ASSETS, ref['path']] = packed
        self.store.objects[audit.ASSETS, catalog_ref['path']] = raw
        self.store.objects[audit.ASSETS, path] = publication.encode(promotion)
        self.paths.append(path)
        return catalog, promotion

    def prepare(self, **kwargs):
        return replacement.prepare(self.store, {OLD: NEW}, promotion_paths=self.paths, **kwargs)

    def test_distinct_histories_keep_their_own_complete_projection(self):
        originals = {}
        unrelated = [OLD.rsplit('/', 1)[0] + '-sibling/document.pdf'] + [
            'derived/other/' + f'{i:032x}' + '/document.pdf' for i in range(6)]
        index = json.loads(self.store.read_bytes(audit.BUCKET, audit.INDEX))
        template = copy.deepcopy(index['files'][0])
        for i, path in enumerate(unrelated):
            entry = index['files'][0] if i == 0 else copy.deepcopy(template)
            if i:
                entry.update(key=f'VoiceOfML/Test\0unrelated-{i}.pdf',
                             new_path=NEW.replace('c' * 16, f'{i:016x}'))
                index['files'].append(entry)
            entry['previous_pdf'] = {'bucket': audit.BUCKET, 'path': path}
            self.store.api.files[path] = {'size': 55, 'xet_hash': f'unrelated-{i}'}
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        other_old = 'derived/other/' + 'e' * 32 + '/document.pdf'
        other_new = NEW.replace('c' * 16, 'e' * 16)
        for i in range(2):
            base = copy.deepcopy(self.base)
            base['historical_metadata'] = {'edition': i}
            base['f'][KEY].update(pd=OLD, pdb=audit.BUCKET, o=OLD, ob=audit.BUCKET,
                                  c=OLD, cb=audit.BUCKET, f=OLD, fb=audit.BUCKET)
            base['f'][f'VoiceOfML/Test\0alias-{i}.pdf'] = {'s': 2, 'p': OLD, 'b': audit.BUCKET}
            for j, path in enumerate(unrelated):
                base['f'][f'VoiceOfML/Test\0previous-{j}.pdf'] = {
                    's': 2, 'b': audit.BUCKET, 'p': path,
                    'previous_pdf': {'bucket': audit.BUCKET, 'path': path}}
            override_key = 'VoiceOfML/Test\0override.pdf'
            files = {override_key: {
                'resource': publication.metadata(audit.BUCKET,
                    NEW.rsplit('/', 1)[0] + '/reading-manifest.json', b'fixture'),
                'source_sha256': 'a' * 64, 'reading_generation': 'e' * 64}}
            catalog, _ = self.history(base, files=files,
                suppressed_sources=['VoiceOfML/Test\0suppressed.pdf'],
                derived_pdf_redirects={OLD: NEW, other_old: other_new},
                metadata={'edition': i, 'previous_pdf': unrelated})
            originals[catalog['generation']] = (base, catalog)
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        self.assertEqual(plan, self.prepare())
        self.assertEqual(len(plan['replacements']), 2)
        self.assertEqual(plan['audit']['unindexed_legacy_pdfs'], sorted(unrelated))
        self.assertEqual(len({item['generation'] for item in plan['replacements']}), 2)
        self.assertEqual(len({item['snapshot_ref']['path'] for item in plan['replacements']}), 2)
        for item in plan['replacements']:
            base, original = originals[item['old_generation']]
            actual_base = audit.decode_sidecar(item['packed'])
            actual = item['catalog']
            expected, _ = redirects.rewrite(finalization._projection(base, original), {OLD: NEW})
            self.assertEqual(finalization._projection(actual_base, actual), expected)
            for field in ('files', 'suppressed_sources', 'metadata', 'parent_generation'):
                self.assertEqual(actual[field], original[field])
            self.assertEqual(actual[redirects.FIELD], {other_old: other_new})
            self.assertEqual(actual['rollback_target'], original['generation'])
            self.assertEqual(item['catalog_ref']['path'],
                f"{replacement.PREFIX}{original['generation']}/{actual['generation']}/catalog.json")
            for field in ('old_catalog', 'old_promotion'):
                ref = item[field]
                raw = before[ref['bucket'], ref['path']]
                self.assertEqual(ref['sha256'], finalization._sha(raw))
                self.assertEqual(ref['bytes'], len(raw))
            for path in unrelated:
                self.assertIn(path, json.dumps(actual_base))
            self.assertEqual(item['redirects_removed'], 1)
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.writes, [])

    def test_full_238_mapping_is_audited_without_expanding_to_unrelated_paths(self):
        template = fixture()[0]['files'][0]
        index = {'version': 1, 'kind': 'pdf-derived-source-index', 'files': []}
        mapping, base = {}, {'v': 1, 'f': {}}
        self.store.api.files.clear()
        for i in range(238):
            sha = f'{i:064x}'
            old = f'derived/scope/{i:032x}/document.pdf'
            new = f'objects/{sha[:2]}/{sha}/{i:016x}/document.pdf'
            key = f'VoiceOfML/Test\0book-{i}.pdf'
            entry = {**template, 'key': key, 'source_path': f'book-{i}.pdf',
                     'source_sha256': sha, 'new_path': new,
                     'manifest_path': new.rsplit('/', 1)[0] + '/derived-manifest.json'}
            index['files'].append(entry)
            raw = audit.encode({**entry, 'version': 1, 'kind': 'pdf-derived-source'})
            self.store.objects[audit.BUCKET, entry['manifest_path']] = raw
            self.store.api.files.update({old: {'size': 55, 'xet_hash': f'copy-{i}'},
                new: {'size': 55, 'xet_hash': f'copy-{i}'},
                entry['manifest_path']: {'size': len(raw), 'xet_hash': f'manifest-{i}'}})
            mapping[old] = new
            base['f'][key] = {'s': 2, 'b': audit.BUCKET, 'p': old}
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        self.history(base, derived_pdf_redirects=mapping)
        before = copy.deepcopy(self.store.objects)
        plan = replacement.prepare(self.store, mapping, promotion_paths=self.paths)
        self.assertEqual(len(plan['audit']['planned']), 238)
        item = plan['replacements'][0]
        self.assertEqual(item['changed_routes'], 238)
        self.assertEqual(item['redirects_removed'], 238)
        self.assertNotIn(redirects.FIELD, item['catalog'])
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.writes, [])

    def test_young_unacked_history_can_plan_without_any_writes_or_fake_acks(self):
        self.history()
        before = copy.deepcopy(self.store.objects)
        # This minimal wrapper makes mutation unavailable, including journals.
        class ReadOnlyStore:
            api = self.store.api
            read_bytes = self.store.read_bytes
            def list_files(inner, bucket, prefixes):
                return {path for b, path in before if b == bucket and path.startswith(prefixes)}
        plan = replacement.prepare(ReadOnlyStore(), {OLD: NEW})
        self.assertEqual(len(plan['replacements']), 1)
        for flag in ('applied', 'consumer_acks_created', 'objects_deleted', 'deletion_authorized'):
            self.assertFalse(plan[flag])
        self.assertNotIn('acks', plan['replacements'][0]['catalog'])
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.writes, [])

    def test_wrong_bucket_and_unsupported_selected_bundle_references_reject(self):
        root = OLD.rsplit('/', 1)[0]
        for mode in ('wrong-bucket', 'unknown-path', 'manifest', 'bundle-root', 'catalog', 'override'):
            with self.subTest(mode=mode):
                base = copy.deepcopy(self.base)
                fields = {}
                if mode == 'wrong-bucket':
                    base['f'][KEY]['b'] = audit.ASSETS
                elif mode == 'unknown-path':
                    base['f'][KEY]['unknown'] = OLD
                elif mode == 'manifest':
                    base['f'][KEY]['previous_pdf'] = {'path': root + '/derived-manifest.json'}
                elif mode == 'bundle-root':
                    base['metadata'] = root
                elif mode == 'catalog':
                    fields['metadata'] = {'path': OLD}
                else:
                    fields['files'] = {'override': {
                        'resource': publication.metadata(audit.BUCKET,
                            NEW.rsplit('/', 1)[0] + '/reading-manifest.json', b'x'),
                        'source_sha256': 'a' * 64, 'reading_generation': 'e' * 64,
                        'previous_pdf': {'path': root + '/pages/page-000001.png'}}}
                self.paths = []
                self.history(base, **fields)
                with self.assertRaises(ValueError):
                    self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_bad_copy_missing_target_and_bad_manifest_reject(self):
        self.history()
        files, stored = copy.deepcopy(self.store.api.files), copy.deepcopy(self.store.objects)
        for mode in ('hash', 'size', 'missing', 'manifest'):
            with self.subTest(mode=mode):
                self.store.api.files = copy.deepcopy(files)
                self.store.objects = copy.deepcopy(stored)
                if mode == 'hash':
                    self.store.api.files[NEW]['xet_hash'] = 'foreign'
                elif mode == 'size':
                    self.store.api.files[OLD]['size'] += 1
                elif mode == 'missing':
                    del self.store.api.files[NEW]
                else:
                    self.store.objects[audit.BUCKET, NEW.rsplit('/', 1)[0] + '/derived-manifest.json'] = b'{}'
                with self.assertRaises(ValueError):
                    self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_unknown_or_corrupt_historical_refs_reject(self):
        catalog, promotion = self.history()
        before = copy.deepcopy(self.store.objects)
        for mode in ('extra', 'bucket', 'path', 'hash', 'generation', 'snapshot', 'unknown-field'):
            with self.subTest(mode=mode):
                self.store.objects = copy.deepcopy(before)
                value = copy.deepcopy(promotion)
                if mode == 'extra':
                    value['resources'].append(copy.deepcopy(value['resources'][0]))
                elif mode == 'generation':
                    value['generation'] = 'f' * 64
                elif mode == 'unknown-field':
                    value['unexpected'] = {'path': OLD.rsplit('/', 1)[0] + '/derived-manifest.json'}
                elif mode == 'snapshot':
                    self.store.objects[audit.ASSETS, catalog['base_sidecar']['path']] = b'foreign'
                else:
                    value['resources'][0][{'bucket': 'bucket', 'path': 'path', 'hash': 'sha256'}[mode]] = 'foreign'
                self.store.objects[audit.ASSETS, self.paths[0]] = publication.encode(value)
                with self.assertRaises(ValueError):
                    self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_supplied_proof_is_checked_and_does_not_bypass_audit(self):
        self.history()
        proof = audit.audit(self.store, self.store.api, active_redirects={OLD: NEW})
        self.assertEqual(self.prepare(), self.prepare(audited_proof={'audit': proof}))
        wrong = copy.deepcopy(proof)
        wrong['planned'][0]['new_path'] = NEW.replace('c' * 16, 'd' * 16)
        with self.assertRaises(ValueError):
            self.prepare(audited_proof=wrong)
        self.store.api.files[NEW]['xet_hash'] = 'foreign'
        with self.assertRaises(ValueError):
            self.prepare(audited_proof=proof)
        self.assertEqual(self.store.writes, [])

    def test_already_rewritten_history_removes_only_selected_redirects(self):
        base, _ = redirects.rewrite(self.base, {OLD: NEW})
        self.history(base, derived_pdf_redirects={OLD: NEW})
        item = self.prepare()['replacements'][0]
        self.assertEqual(item['changed_routes'], 0)
        self.assertEqual(item['redirects_removed'], 1)
        self.assertEqual(audit.decode_sidecar(item['packed']), base)
        self.assertNotIn(redirects.FIELD, item['catalog'])
        self.paths = []
        self.history(base)
        self.assertEqual(self.prepare()['replacements'], [])

    def test_promotion_change_during_planning_rejects(self):
        self.history()
        read = self.store.read_bytes
        path = self.paths[0]
        reads = 0
        def changed(bucket, name):
            nonlocal reads
            raw = read(bucket, name)
            if name == path:
                reads += 1
                if reads > 1:
                    return raw + b' '
            return raw
        with patch.object(self.store, 'read_bytes', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'evidence changed'):
                self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_current_generation_is_skipped_or_rejected_when_explicit(self):
        catalog, promotion = self.history()
        ref = promotion['resources'][0]
        self.store.objects[audit.ASSETS, publication.POINTER] = publication.encode({
            'version': 1, 'kind': 'reader-reading-pointer', 'generation': catalog['generation'],
            'catalog': ref, 'resources': [ref]})
        with self.assertRaisesRegex(ValueError, 'current generation'):
            self.prepare()
        with patch.object(self.store, 'list_files', create=True, return_value=self.paths):
            self.assertEqual(replacement.prepare(self.store, {OLD: NEW})['replacements'], [])
        self.assertEqual(self.store.writes, [])

    def test_full_projection_metadata_drift_rejects(self):
        self.history()
        project = publication.project_sidecar
        def drift(base, catalog):
            value = project(base, catalog)
            if 'rollback_target' in catalog:
                value['unexpected'] = 'drift'
            return value
        with patch.object(publication, 'project_sidecar', side_effect=drift):
            with self.assertRaisesRegex(ValueError, 'full Reader projection'):
                self.prepare()

    def test_conflicting_immutable_outputs_reject_matching_outputs_are_reusable(self):
        self.history()
        plan = self.prepare()
        item = plan['replacements'][0]
        for ref_field, raw_field in (('snapshot_ref', 'packed'), ('catalog_ref', 'catalog_raw')):
            ref = item[ref_field]
            self.store.objects[ref['bucket'], ref['path']] = b'foreign'
            with self.assertRaisesRegex(ValueError, 'immutable history replacement conflict'):
                self.prepare()
            self.store.objects[ref['bucket'], ref['path']] = item[raw_field]
            self.assertEqual(plan, self.prepare())
        self.assertEqual(self.store.writes, [])


if __name__ == '__main__':
    unittest.main()
