import gzip
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from scripts import publish_reader_v3 as v3, reader_image_history as image
from scripts import reader_image_history_catalog as catalog, reader_image_history_publish as legacy
from scripts import reader_image_history_runtime as runtime, reader_image_history_shard as shard
from scripts import reader_image_history_transition as indexes, reader_lifecycle as lifecycle
from scripts.reader_stream_checkpoint_cas import ConditionalCheckpointStore
from tests.test_reader_image_history_transition import Bucket
from tests.test_reader_image_history_shard import snapshot


class MemoryLegacyJournal:
    def __init__(self):
        self.records = {}

    def read(self, key):
        return self.records.get(key)

    def claim(self, key, record):
        return self.advance(key, None, record)

    def advance(self, key, expected, record):
        if self.read(key) != expected:
            return False
        self.records[key] = record
        return True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.key = 'Owner/Repo\0archive/h.PNG'
        now = '2026-11-21T00:00:00+00:00'
        operation = 'a' * 32
        source = image.Source('Owner/Repo', 'archive/h.PNG', 'a' * 40, image._sha(b'png'), 3)
        current = image.Source(source.repo, source.path, 'b' * 40, source.sha256, source.size)
        self.old_raw, self.new_raw = b'fake webp', b'fake avif'
        old_root = f'pages/image/png/{source.sha256}'
        address = {'version': 1, 'source_sha256': source.sha256, 'files': {'document.avif': image._sha(self.new_raw)}}
        new_root = f'pages/image/png/{image._sha(v3.encode(address))}'
        self.old = {'key': self.key, 'repo': source.repo, 'source_path': source.path,
                    'source_revision': source.revision, 'source_sha256': source.sha256, 'source_bytes': source.size,
                    'extension': 'png', 'bucket': image.BUCKET, 'mode': 'image', 'page_count': 1,
                    'path': old_root + '/document.webp', 'sha256': image._sha(self.old_raw), 'bytes': len(self.old_raw)}
        self.new = {**self.old, 'profile': image.AVIF_PROFILE, 'root': new_root, 'path': new_root + '/document.avif',
                    'source_revision': current.revision, 'sha256': image._sha(self.new_raw), 'bytes': len(self.new_raw)}
        webp, avif = (image.Asset(row['path'].rsplit('/', 1)[0], row['path'], row['sha256'], row['bytes'])
                     for row in (self.old, self.new))
        index = {'kind': 'image-stream-index', 'version': 1, 'files': [self.old], 'failures': []}
        canonical = snapshot(index, '"canonical-parent"')
        physical = snapshot(index | {'physical': True}, '"shard-parent"')
        self.shard_path = f'pages/image/index-v2-{shard.shard_for(self.key, 8):02}.json'
        pin = shard.ShardPin(self.shard_path, physical.raw_sha256, len(physical.raw), physical.etag)
        candidate = image._replacement(index, self.key, image.row_digest(self.old), self.new, source=source,
                                       current=current, webp=webp, avif=avif, operation=operation)
        splice = shard.splice(key=self.key, old_row_sha256=image.row_digest(self.old), canonical_target=candidate,
                              canonical_sha256=image._sha(candidate), shard=physical, pin=pin).target
        before, after = ({'v': 1, 'f': {self.key: image._route(asset)}} for asset in (webp, avif))
        self.before, self.after = [gzip.compress(v3.encode(value), mtime=0) for value in (before, after)]
        parent, _ = lifecycle.catalog_generation(before, now=now)
        self.archive = v3.encode(parent)
        target = catalog.plan(io.BytesIO(self.archive), io.BytesIO(), key=self.key, webp_path=webp.path,
                              avif_path=avif.path, before_sidecar=self.before, after_sidecar=self.after, now=now)
        legacy_preview = target | {'operation': operation, 'parent_etag': '"catalog-parent"', 'timestamp': now}

        def base_ref(raw):
            return v3.metadata(v3.ASSETS, f'reader-index/snapshots/{image._sha(raw)}/reader_assets.json.gz', raw)

        old_base, proposed = map(base_ref, (self.before, self.after))
        reading = {'kind': 'reader-reading-catalog', 'version': 3, 'files': {},
                   'base_sidecar': old_base, 'parent_generation': None}
        original = v3.commit_catalog(self, reading, None)['pointer']
        self.original = v3.encode(original)
        next_catalog = reading | {'base_sidecar': proposed, 'parent_generation': original['generation']}
        del next_catalog['generation']
        successor = v3.commit_catalog(self, next_catalog, original)['pointer']
        self.successor_raw = v3.encode(next_catalog)
        report_pair = ('before_projection_sha256', 'after_projection_sha256')
        self.projections = v3.project_sidecar(before, reading), v3.project_sidecar(after, reading)
        pair = dict(zip(report_pair, image._projection_delta(self.key, webp, avif, *self.projections)))
        search_proof = {'some': 'reviewed Search proof'}
        report = {'kind': 'reader-image-history-preview', 'version': 1, 'key': self.key, 'operation': operation,
                  'changed_route_keys': [self.key], 'producer_revision': shard.PRODUCER, 'deletion_authorized': False,
                  'old_row': self.old, 'target_row': self.new, 'old_row_sha256': image.row_digest(self.old),
                  'search_proof': search_proof, 'search_sha256': image._sha(image._encode(search_proof)),
                  'source': asdict(source), 'current_source': asdict(current),
                  'before_route': image._route(webp), 'after_route': image._route(avif), **pair,
                  'parent_sidecar_sha256': image._sha(self.before), 'sidecar_sha256': image._sha(self.after),
                  'reader_pointer_sha256': image._sha(self.original), 'reader_generation': original['generation'],
                  'reader_catalog_sha256': original['catalog']['sha256'], 'pinned_base_sha256': old_base['sha256'],
                  'proposed_base': proposed, 'shard': asdict(pin) | {'selected_row': self.old},
                  'canonical': {'path': image.CANONICAL, 'bytes': len(canonical.raw), 'etag': canonical.etag,
                                'sha256': canonical.raw_sha256, 'target_sha256': image._sha(candidate)}}
        shard_preview = {'applied': False, 'actual_producer': shard.PRODUCER, 'operation': operation,
                         'parent': asdict(pin), 'canonical_shadow_sha256': image._sha(candidate),
                         'target_sha256': image._sha(splice), 'target_bytes': len(splice)}
        v3_preview = {'operation': operation, 'applied': False, 'deletion_authorized': False, 'key': self.key,
                      'expected_base': proposed, 'expected_canonical_sidecar': image._sha(self.after),
                      'generation': successor['generation'], 'parent_generation': original['generation'],
                      'pointer': successor, **pair}
        reports = [v3.encode(r) for r in (report, shard_preview, legacy_preview, v3_preview)]
        self.digest = image._sha(v3.encode(list(zip(runtime.REPORTS, map(image._sha, reports)))))
        files = dict(zip(runtime.REPORTS, reports)) | {
            'candidate.json': candidate, 'owning-shard-splice.json': splice, 'document.avif': self.new_raw,
            'sidecar.json.gz': self.after, 'pinned-base.json.gz': self.after}
        for name, raw in files.items():
            (self.dir / name).write_bytes(raw)
        self.review_inputs = dict(reviewed_sha256=self.digest, canonical_parent=canonical.raw, shard_parent=physical.raw,
                                  before_sidecar=self.before, sidecar_etag='"sidecar-parent"', pointer_parent=self.original)
        self.reviewed = self.review()
        self.bucket = Bucket()
        self.bucket.objects = {image.CANONICAL: (canonical.raw, canonical.etag),
                               self.shard_path: (physical.raw, physical.etag)}
        self.index_journal = indexes.S3TransitionJournal(self.bucket)
        self.legacy_journal = MemoryLegacyJournal()
        self.reader = {v3.POINTER: self.original, original['catalog']['path']: v3.encode(reading),
                       old_base['path']: self.before, legacy.SIDECAR: self.before, self.old['path']: self.old_raw,
                       legacy.CATALOG: self.archive}
        self.tags = {legacy.CATALOG: legacy_preview['parent_etag'], legacy.SIDECAR: '"sidecar-parent"'}
        self.events, self.catalog_gets, self.resolver_gets = [], 0, 0
        self.fenced, self.fail = True, None

    def review(self, **changes):
        return runtime.review(self.dir, **(self.review_inputs | changes))

    def read_bytes(self, bucket, path):
        self.assertEqual(bucket, image.BUCKET)
        if path not in self.reader:
            raise FileNotFoundError(path)
        return self.reader[path]

    def guard(self, phase):
        self.events.append(('guard', phase))
        return self.fenced

    def create(self, bucket, path, raw):
        self.assertEqual(bucket, image.BUCKET)
        self.assertNotIn(path, self.reader)
        if path == self.new['path']:
            self.assertIn(self.reviewed.processing_path, self.reader)
        self.events.append(('create', path))
        self.reader[path] = raw

    def read_legacy(self, path):
        if path == legacy.CATALOG:
            self.catalog_gets += 1
        raw = self.reader[path]
        return legacy.Version(image._sha(raw), len(raw), self.tags[path])

    def reader_catalog_sha(self):
        self.resolver_gets += 1
        return image._sha(self.reader[legacy.CATALOG])

    def cas(self, path, stream, *, size, digest, parent_etag):
        self.assertEqual(self.tags[path], parent_etag)
        self.assertEqual(self.index_journal.load(self.reviewed.transition.claim_key)[1].state, 'applied')
        self.assertEqual(json.loads(self.reader[self.reviewed.processing_path])['status'], 'processing')
        raw = stream.read()
        self.assertEqual((len(raw), image._sha(raw)), (size, digest))
        self.events.append(('cas', path))
        if self.fail == (path, 'before'):
            raise OSError('lost legacy PUT')
        self.reader[path], self.tags[path] = raw, f'"{digest}"'
        if self.fail == (path, 'after'):
            raise OSError('lost legacy PUT')
        return self.tags[path]

    def run_legacy(self, **changes):
        args = dict(open_catalog_parent=lambda: io.BytesIO(self.archive), checkpoints=ConditionalCheckpointStore(self.bucket),
                    index_journal=self.index_journal, legacy_journal=self.legacy_journal, reader_store=self,
                    observe_legacy=self.read_legacy, legacy_etag=self.tags.__getitem__,
                    reader_catalog_digest=self.reader_catalog_sha, legacy_cas=self.cas, guard=self.guard,
                    revalidate=lambda reviewed, _: image.Checks(reviewed.transition.canonical.preflight, *self.projections),
                    create_immutable=self.create, observe_avif_mime=lambda *_: 'image/avif')
        return runtime.publish_legacy(self.reviewed, **(args | changes))

    def test_exact_publication_and_recovery_observation(self):
        result = self.run_legacy()
        self.assertEqual([path for action, path in self.events if action in {'create', 'cas'}],
                         [self.reviewed.processing_path, self.new['path'], self.reviewed.legacy_snapshot,
                          legacy.CATALOG, legacy.SIDECAR])
        self.assertEqual(self.bucket.objects[image.CANONICAL][0], self.reviewed.transition.canonical.candidate)
        self.assertEqual(self.bucket.objects[self.shard_path][0], self.reviewed.transition.shard_target)
        self.assertEqual(self.catalog_gets, 2)
        self.assertEqual(self.resolver_gets, 3)
        self.assertFalse(result['reader_consumers_verified'])
        self.assertFalse(result['deletion_authorized'])
        self.assertTrue(result['unify']['conditional_pointer'])
        self.assertEqual(v3.encode(v3.unify(self, **result['unify'])['pointer']), self.reviewed.pointer_target)
        self.assertEqual(self.run_legacy(resume_indexes=True, resume_legacy=True), result)
        self.assertEqual([path for action, path in self.events if action == 'cas'], [legacy.CATALOG, legacy.SIDECAR])

    def test_binds_reports_artifacts_and_archived_parent(self):
        for fields in ({'before_sidecar': self.before + b'\n'}, {'shard_parent': b'wrong'}, {'pointer_parent': b'wrong'}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, 'exact reviewed'):
                self.review(**fields)
        with self.assertRaisesRegex(ValueError, 'archived legacy catalog parent'):
            self.run_legacy(open_catalog_parent=lambda: io.BytesIO(b'!' + self.archive[1:]))
        for name, error in (('candidate.json', 'canonical target'), ('owning-shard-splice.json', 'shard target'),
                            ('document.avif', 'AVIF'), ('legacy-catalog-preview.json', 'independently reviewed digest')):
            path = self.dir / name
            raw = path.read_bytes()
            path.write_bytes(b'!' + raw[1:])
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, error):
                self.review()
            path.write_bytes(raw)
        self.assertEqual(self.bucket.puts, [])
        self.assertFalse(self.legacy_journal.records)
        self.assertEqual([e for e in self.events if e[0] in {'cas', 'create'}], [])

    def test_protection_or_actual_fence_failure_stops_index_writes(self):
        self.fenced = False
        with self.assertRaisesRegex(ValueError, 'actual image history writer/GC fence'):
            self.run_legacy()
        self.assertEqual(self.catalog_gets, 0)
        self.fenced = True
        self.reader[self.reviewed.processing_path] = b'{"status":"released"}'
        with self.assertRaisesRegex(ValueError, 'active AVIF processing root'):
            self.run_legacy()
        self.assertEqual(self.bucket.puts, [])
        self.assertNotIn(self.new['path'], self.reader)

    def test_wrong_avif_serving_mime_blocks_every_mutable_cas(self):
        with self.assertRaisesRegex(ValueError, 'served AVIF MIME'):
            self.run_legacy(observe_avif_mime=lambda *_: 'application/octet-stream')
        self.assertEqual(self.bucket.puts, [])
        self.assertFalse(self.legacy_journal.records)

    def test_lost_catalog_and_sidecar_puts_resume_without_replay(self):
        self.fail = legacy.CATALOG, 'after'
        with self.assertRaisesRegex(OSError, 'lost legacy PUT'):
            self.run_legacy()
        self.fail = legacy.SIDECAR, 'after'
        with self.assertRaisesRegex(OSError, 'lost legacy PUT'):
            self.run_legacy(resume_indexes=True, resume_legacy=True)
        self.assertEqual(self.run_legacy(resume_indexes=True, resume_legacy=True)['sidecar'].sha256, image._sha(self.after))
        self.assertEqual([path for action, path in self.events if action == 'cas'], [legacy.CATALOG, legacy.SIDECAR])

    def test_spent_catalog_permit_with_unchanged_parent_does_not_replay(self):
        self.fail = legacy.CATALOG, 'before'
        with self.assertRaisesRegex(OSError, 'lost legacy PUT'):
            self.run_legacy()
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_legacy(resume_indexes=True, resume_legacy=True)
        self.assertEqual([path for action, path in self.events if action == 'cas'], [legacy.CATALOG])

    def test_resolver_checked_again_after_sidecar_permit_before_cas(self):
        def resolver():
            if self.legacy_journal.read(self.reviewed.catalog_parent.sha256).state == 'sidecar-permitted':
                return image._sha(self.archive)
            return self.reader_catalog_sha()

        with self.assertRaisesRegex(ValueError, 'catalog reader view differs'):
            self.run_legacy(reader_catalog_digest=resolver)
        self.assertEqual([path for action, path in self.events if action == 'cas'], [legacy.CATALOG])
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_legacy(resume_indexes=True, resume_legacy=True)

    def test_no_v3_admission_until_independent_catalog_view_matches_after_sidecar(self):
        def resolver():
            return self.reader_catalog_sha() if self.reader[legacy.SIDECAR] == self.before else image._sha(self.archive)

        with self.assertRaisesRegex(ValueError, 'catalog reader view differs'):
            self.run_legacy(reader_catalog_digest=resolver)
        self.assertEqual(self.legacy_journal.read(self.reviewed.catalog_parent.sha256).state, 'sidecar-permitted')
        self.assertEqual(self.run_legacy(resume_indexes=True, resume_legacy=True)['sidecar'].sha256, image._sha(self.after))

    def test_observation_requires_both_independent_consumer_receipts(self):
        self.run_legacy()
        pointer = json.loads(self.reviewed.pointer_target)
        self.reader[v3.POINTER] = self.reviewed.pointer_target
        self.reader[pointer['catalog']['path']] = self.successor_raw
        self.reader[self.reviewed.report['proposed_base']['path']] = self.after

        def receipt(surface):
            return {'version': 1, 'surface': surface, 'active': True, 'projection_verified': True,
                    'checked_url': 'fixture', 'files': 0, 'catalog_sha256': pointer['catalog']['sha256'],
                    'generation': pointer['generation']}

        with self.assertRaisesRegex(ValueError, 'pages did not verify'):
            runtime.observe_v3(self.reviewed, reader_store=self, guard=self.guard,
                               observe_receipt=lambda surface: receipt(surface) | {'active': surface == 'hf'})
        result = runtime.observe_v3(self.reviewed, reader_store=self, guard=self.guard, observe_receipt=receipt)
        self.assertEqual(set(result['consumers']), {'hf', 'pages'})
        self.assertTrue(result['reader_consumers_verified'])
        self.assertFalse(result['deletion_authorized'])
