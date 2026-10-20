"""Complete tiny OCR bundles and durable automation contracts; offline, no models."""
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import auto_ccrd_ocr as auto, ccrd_ocr_release as contract
from scripts import export_reader_txt as exporter, pdf_reading_v3 as v3, pdf_text_layer as text, shared
from scripts import publish_ccrd_ocr as publisher
from scripts import verify_ccrd_ocr_live as live
from tests.test_pdf_text_layer import raw_page
from tests.test_publish_ccrd_ocr import Store


APPROVED = {'version': 1, 'consumer_revision': 'a' * 40,
            'builder_sha256': 'b' * 64, 'base_metadata_sha256': 'c' * 64}


class AutoCcrdOcrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store()
        self.registry = {'version': 1, 'files': {}}
        self.derived = {'version': 1, 'kind': 'pdf-derived-source-index', 'files': []}
        self.empty_catalog = (None, {'files': {}})

    def book(self, name, *, source='a', accepted=False):
        path = '目录/' + name + '.pdf'
        key = 'VoiceOfML/Books\0' + path
        layers = [text.from_page({**raw_page(body), 'page': number}, source * 64)
                  for number, body in enumerate(('手机原始全文', '', '手。机末页 ABC'), 1)]
        if accepted:
            first = layers[0]
            layers[0] = text.accept_proposal(first, {'version': 1, 'kind': 'pdf-text-correction',
                'base_generation': first['generation'], 'raw_sha256': first['raw_sha256'],
                'page_identity': first['page_identity'], 'replacements': [
                    {'region_id': first['regions'][0]['id'], 'before': first['text'], 'after': '手机已接受全文'}]},
                actor='fixture-reviewer')
        bundle = self.root / ('bundle-' + name)
        ref = v3.build_text_bundle(layers, [{'p': n} for n in range(1, 4)],
                                  Path('objects') / (source * 2) / (source * 64) / ('d' * 16), bundle)
        for target in bundle.rglob('*'):
            if target.is_file():
                self.store.objects[shared.PDF_PAGES_BUCKET, target.relative_to(bundle).as_posix()] = target.read_bytes()
        entry = {'repo': 'VoiceOfML/Books', 'path': path, 'key': key, 'status': 'ready',
                 'source_kind': 'upstream', 'source_revision': 'e' * 40,
                 'source_sha256': source * 64, 'source_bytes': 100, 'page_count': 3, 'text_layer': ref}
        self.registry['files'][key] = entry
        return key, entry

    def inputs(self):
        self.store.objects[shared.READER_ASSETS_BUCKET, auto.REGISTRY] = contract.canonical(self.registry)
        self.store.objects[shared.PDF_PAGES_BUCKET, auto.DERIVED] = contract.canonical(self.derived)

    def run_auto(self, name, **kwargs):
        self.inputs()
        with patch.object(auto.reading, 'current', return_value=self.empty_catalog):
            return auto.reconcile(self.store, self.root / name, self.root / 'consumer', APPROVED, **kwargs)

    def build(self, root, refs, consumer_root, builder_sha, base_sha):
        self.assertEqual(builder_sha, APPROVED['builder_sha256'])
        self.assertEqual(base_sha, APPROVED['base_metadata_sha256'])
        refs = sorted(refs, key=lambda ref: ref['path'])
        records, exports = zip(*(contract.export_record(root, ref) for ref in refs))
        identity = {'version': 1, 'kind': 'ccrd-ocr-search-release', 'base_metadata_sha256': base_sha,
                    'builder_sha256': builder_sha, 'tokenizer_version': contract.TOKENIZER, 'exports': refs}
        generation = contract.digest(contract.canonical(identity))
        db_path = f'indexes/ocr/{generation}/OCR.sqlite3'
        db = b'bounded fake database; real builder is delegated without modification'
        destination = root / db_path
        destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(db)
        manifest = {**identity, 'generation': generation, 'documents': len(refs), 'database': publisher.resource(db_path, db)}
        path = f'releases/ocr-{generation}/manifest.json'
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(contract.canonical(manifest))
        self.candidate = {'manifest': manifest, 'records': list(records), 'exports': list(exports)}
        return {'manifest': manifest, 'path': path, 'sha256': contract.digest(destination.read_bytes())}

    def plan(self, root, path, digest, base):
        refs = [*self.candidate['manifest']['exports'], self.candidate['manifest']['database'],
                *[value['text'] for value in self.candidate['exports']]]
        files = {path: (root / path).read_bytes(), **{ref['path']: (root / ref['path']).read_bytes() for ref in refs}}
        return self.candidate, files

    def test_unreviewed_admission_then_aggregate_keeps_old_book_and_id(self):
        first, _ = self.book('first')
        with patch.object(auto.builder, 'build_release', side_effect=self.build), \
                patch.object(publisher, 'plan', side_effect=self.plan), patch.object(publisher, 'assert_writer'):
            one = self.run_auto('first-run', apply=True, limit=1)
            self.assertEqual(one['processed'][0]['quality'], 'unreviewed')
            self.assertEqual(one['processed'][0]['accepted_pages'], 0)
            self.assertEqual(one['processed'][0]['doc_id'], 'OCR:1')
            previous = copy.deepcopy(self.candidate)
            second, _ = self.book('second', source='f')
            two = self.run_auto('second-run', apply=True, limit=1)
            self.assertEqual(two['documents'], 2)
            self.assertEqual(two['processed'][0]['key'], second)
            self.assertEqual(two['processed'][0]['doc_id'], 'OCR:2')
            self.assertIn(previous['manifest']['exports'][0], self.candidate['manifest']['exports'])
            self.assertIn(first, self.registry['files'])
            writes = list(self.store.writes)
            three = self.run_auto('third-run', apply=True)
            self.assertTrue(three['unchanged'])
            self.assertEqual(self.store.writes, writes)

    def test_plan_only_has_no_writes_or_text_page_reads(self):
        key, _entry = self.book('raw')
        with patch.object(auto.builder, 'build_release') as build, patch.object(exporter, 'build_export') as export:
            result = self.run_auto('plan', plan_only=True)
        self.assertEqual(result['selected'], [key])
        self.assertEqual(self.store.writes, [])
        build.assert_not_called(); export.assert_not_called()

    def test_exact_pilot_does_not_admit_an_unrelated_ready_book(self):
        self.book('first')
        key, entry = self.book('second', source='f')
        result = self.run_auto('scoped-plan', plan_only=True, source={'repo': entry['repo'], 'path': entry['path']})
        self.assertEqual(result['selected'], [key])
        self.assertEqual(self.store.writes, [])

    def test_failure_quarantines_exact_input_without_publishing_partial_book(self):
        key, entry = self.book('bad')
        del self.store.objects[entry['text_layer']['bucket'], entry['text_layer']['path']]
        with patch.object(publisher, 'assert_writer'), patch.object(auto.builder, 'build_release') as build:
            with self.assertRaises(FileNotFoundError):
                self.run_auto('failed', apply=True)
            self.assertNotIn((contract.BUCKET, publisher.CURRENT), self.store.objects)
            report = self.run_auto('quarantined', plan_only=True)
            self.assertEqual(report['quarantined'], [key])
            self.assertEqual(report['eligible'], 0)
            retried = self.run_auto('retry-plan', plan_only=True, retry_failures=True)
            self.assertEqual(retried['selected'], [key])
            build.assert_not_called()

    def test_quarantine_identity_ignores_unrelated_live_release_changes(self):
        """An unchanged bad input stays quarantined when only the catalog moves."""
        key, entry = self.book('stable')
        jobs, _, _ = auto.discover(self.registry, self.derived, {'files': {}}, {}, {'failures': {}})
        quarantine = jobs[0]['quarantine']
        state = {'failures': {key: {'quarantine': quarantine, 'fingerprint': 'old'}}}
        moved = {'files': {key: {'resource': {'path': 'objects/moved/reading-manifest.json'},
                                 'source_sha256': entry['source_sha256'],
                                 'reading_generation': 'f' * 64,
                                 'page_count': entry['page_count'],
                                 'components': {'text': 'ready'}}}}
        jobs, _, quarantined = auto.discover(self.registry, self.derived, moved, {}, state)
        self.assertEqual(jobs, [])
        self.assertEqual(quarantined, [key])
        changed = copy.deepcopy(self.registry)
        changed['files'][key]['source_sha256'] = 'b' * 64
        jobs, _, quarantined = auto.discover(changed, self.derived, {'files': {}}, {}, state)
        self.assertEqual(quarantined, [])
        self.assertEqual([job['key'] for job in jobs], [key])

    def test_missing_companion_is_deferred_not_called_published_or_corrected(self):
        key, entry = self.book('legacy')
        del entry['text_layer']
        result = self.run_auto('legacy-plan', plan_only=True)
        self.assertEqual(result['eligible'], 0)
        self.assertEqual(result['deferred'][0]['key'], key)
        self.assertEqual(result['model_calls'], 0)

    def test_derived_original_is_not_confused_with_reader_primary(self):
        key, entry = self.book('derived')
        entry['source_kind'] = 'generated'
        derivation = {'version': 1, 'kind': 'pdf-derived-source', 'repo': entry['repo'],
                      'source_path': entry['path'], 'source_revision': entry['source_revision'],
                      'source_sha256': 'f' * 64, 'derived_sha256': entry['source_sha256']}
        relative = 'derived/test/book/derived-manifest.json'
        self.store.objects[shared.PDF_PAGES_BUCKET, relative] = contract.canonical(derivation)
        self.derived['files'] = [{'key': key, **derivation, 'bucket': shared.PDF_PAGES_BUCKET,
                                 'manifest_path': relative, 'derived_bytes': entry['source_bytes']}]
        jobs, _, _ = auto.discover(self.registry, self.derived, {'files': {}}, {}, {'failures': {}})
        spec = auto.export_spec(self.store, jobs[0], 1)
        result = exporter.build_export(spec, lambda ref: self.store.read_bytes(ref['bucket'], ref['path']), allow_unreviewed=True)
        self.assertEqual(result['manifest']['origin_sha256'], 'f' * 64)
        self.assertEqual(result['manifest']['source_sha256'], 'a' * 64)
        self.derived['files'][0]['derived_sha256'] = 'e' * 64
        with self.assertRaisesRegex(ValueError, 'original provenance'):
            auto.export_spec(self.store, jobs[0], 1)

    def test_unusable_source_text_is_a_terminal_disposition_not_a_batch_failure(self):
        """Unchanged binary-control sources must not fail every later batch."""
        key, entry = self.book('binary')
        with patch.object(auto.builder, 'build_release', side_effect=self.build), \
                patch.object(publisher, 'plan', side_effect=self.plan), \
                patch.object(publisher, 'assert_writer'), \
                patch.object(exporter, 'build_export',
                             side_effect=exporter.PermanentTextRejection('OCR text contains binary controls')):
            report = self.run_auto('permanent', apply=True)
        self.assertEqual([item['key'] for item in report['failures']], [key])
        self.assertIn('rejected', report['failures'][0])
        self.assertEqual(report['processed'], [])
        state = json.loads(self.store.objects[(contract.BUCKET, auto.STATE)])
        self.assertEqual(state['failures'][key]['error_type'], 'PermanentTextRejection')
        # The next run treats the same unchanged bytes as an explicit quarantine
        # and is not failed by them.
        again = self.run_auto('permanent-again', plan_only=True)
        self.assertEqual(again['eligible'], 0)
        self.assertEqual(again['quarantined'], [key])
        self.assertNotIn((contract.BUCKET, publisher.CURRENT), self.store.objects)

    def test_static_document_manifest_supplies_converted_provenance(self):
        """Office/PostScript PDFs have no entry in the derived-source index."""
        key, entry = self.book('office')
        original = 'f' * 64
        stem = 'documents/pdf/wps/' + original
        entry.update(source_kind='generated', source_extension='wps',
                     reader_assets_bucket=shared.READER_ASSETS_BUCKET,
                     reader_assets_path=stem + '/document.pdf', source_url='')
        manifest = {'kind': 'static-pdf-document-stream', 'version': 1, 'path': 'document.pdf',
                    'source_extension': 'wps', 'source_sha256': original,
                    'bytes': entry['source_bytes'], 'sha256': entry['source_sha256']}
        path = stem + '/document-manifest.json'
        self.store.objects[shared.READER_ASSETS_BUCKET, path] = contract.canonical(manifest)
        jobs, _, _ = auto.discover(self.registry, self.derived, {'files': {}}, {}, {'failures': {}})
        spec = auto.export_spec(self.store, jobs[0], 1)
        self.assertEqual(spec['origin_sha256'], original)
        self.assertEqual(spec['derivation_manifest']['path'], path)
        read = lambda ref: self.store.read_bytes(ref['bucket'], ref['path'])
        result = exporter.build_export(spec, read, allow_unreviewed=True)
        self.assertEqual(result['manifest']['origin_sha256'], original)
        self.assertEqual(result['manifest']['source_sha256'], entry['source_sha256'])
        manifest['sha256'] = 'c' * 64
        self.store.objects[shared.READER_ASSETS_BUCKET, path] = contract.canonical(manifest)
        with self.assertRaisesRegex(ValueError, 'original provenance'):
            auto.export_spec(self.store, jobs[0], 1)

    def test_only_accepted_descendant_updates_preserve_raw_and_previous_corrections(self):
        key, entry = self.book('upgrade')
        job = {'entry': entry, 'key': key, 'active': None}
        read = lambda ref: self.store.read_bytes(ref['bucket'], ref['path'])
        spec = auto.export_spec(self.store, job, 1)
        original = exporter.build_export(spec, read, allow_unreviewed=True)['manifest']
        raw_ref = entry['text_layer']
        manifest = v3.decode(read(raw_ref))
        partition = v3.decode(read(manifest['partitions'][0]['resource']))
        layer = v3.decode(read(partition['pages'][0]['resource']))
        proposal = {'kind': 'pdf-text-correction', 'version': 1, 'page': 1,
                    'base_generation': layer['generation'], 'raw_sha256': layer['raw_sha256'],
                    'page_identity': layer['page_identity'], 'replacements': [
                        {'region_id': layer['regions'][0]['id'], 'before': layer['text'], 'after': '手机已接受校订'}]}
        bundle = self.root / 'accepted-upgrade'
        accepted_ref = v3.accept_text_bundle(raw_ref, [proposal], read, bundle, actor='fixture-reviewer')
        for target in bundle.rglob('*'):
            if target.is_file():
                self.store.objects[shared.PDF_PAGES_BUCKET, target.relative_to(bundle).as_posix()] = target.read_bytes()
        updated = exporter.build_export({**spec, 'text_manifest': accepted_ref}, read)['manifest']
        publisher.verify_text_upgrade(self.store, original, updated)
        with self.assertRaisesRegex(ValueError, 'downgrade'):
            publisher.verify_text_upgrade(self.store, updated, original)
        with self.assertRaisesRegex(ValueError, 'source identity'):
            publisher.verify_text_upgrade(self.store, original, {**updated, 'doc_id': 'OCR:9'})
        disconnected = copy.deepcopy(updated)
        disconnected['retained_reader_resources'] = original['retained_reader_resources']
        with self.assertRaisesRegex(ValueError, 'descendant'):
            publisher.verify_text_upgrade(self.store, original, disconnected)

    def test_actual_workflow_automates_only_trusted_completion_and_keeps_writer(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/publish-ccrd-ocr.yml').read_text()
        for required in ('schedule:', 'workflow_run:', 'CCRD_OCR_AUTO_ENABLED', 'head_repository.full_name',
                         "head_branch == 'main'", 'group: publish-ccrd-index', 'queue: max',
                         'scripts/auto_ccrd_ocr.py', '--apply --activate-space'):
            self.assertIn(required, workflow)
        self.assertNotIn('OCR_CORRECTION_API', workflow)
        self.assertNotIn('download-artifact', workflow.rsplit('\n  automatic:\n', 1)[1])

    def test_live_acceptance_reads_every_result_page_and_rejects_duplicate_ids(self):
        first = {'total': 201, 'results': [{'doc_id': f'OCR:{number}'} for number in range(1, 201)]}
        last = {'total': 201, 'results': [{'doc_id': 'OCR:201'}]}
        with patch.object(live, 'json_request', side_effect=[first, last]):
            self.assertEqual(len(live.all_results({'sources': ['OCR']})), 201)
        with patch.object(live, 'json_request', side_effect=[first, {'total': 201, 'results': [{'doc_id': 'OCR:1'}]}]):
            with self.assertRaisesRegex(ValueError, 'duplicated'):
                live.all_results({'sources': ['OCR']})

    def test_live_acceptance_derives_legacy_counts_from_archived_metadata(self):
        raw = gzip.compress(contract.canonical({'version': 1, 'sources': [
            {'slug': 'CCRD', 'name': 'CCRD', 'count': 11},
            {'slug': 'CW', 'name': 'CW', 'count': 22},
        ], 'records': []}))
        path = self.root / 'search_data.json.gz'
        path.write_bytes(raw)
        self.assertEqual(live.metadata_counts(path, contract.digest(raw)), {'CCRD': 11, 'CW': 22})
        path.write_bytes(raw + b'x')
        with self.assertRaisesRegex(ValueError, 'differs'):
            live.metadata_counts(path, contract.digest(raw))


if __name__ == '__main__':
    unittest.main()
