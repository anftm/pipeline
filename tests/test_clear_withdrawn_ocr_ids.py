import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import clear_withdrawn_ocr_ids as cleanup


class Reader:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.reader = SimpleNamespace(read_etags={})
    def read_bytes(self, bucket, path):
        if bucket != cleanup.BUCKET or path not in self.objects:
            raise FileNotFoundError(path)
        self.reader.read_etags[(bucket, path)] = '"etag"'
        return self.objects[path]


class Writer:
    def __init__(self, reader):
        self.reader, self.calls, self.lose = reader, [], None
    def put(self, path, raw, *, if_match=None):
        self.calls.append((path, if_match))
        self.reader.objects[path] = raw
        if self.lose == path:
            self.lose = None
            raise cleanup.Unknown('lost response')


class CleanupTests(unittest.TestCase):
    def setUp(self):
        ids = {f'{i:064x}': {'doc_id': f'OCR:{i}', 'readable_rel_path': f'test/{i}.txt'}
               for i in range(1, 782)}
        before = {'version': 1, 'kind': 'ccrd-ocr-id-registry', 'status': 'withdrawn', 'resources': [],
                  'reset_id': 'ccrd-ocr-exact-20261030', 'ids': ids}
        self.before = cleanup.encode(before)
        self.after = cleanup.encode({**before, 'ids': {}})
        self.plan = cleanup.encode({'kind': 'ccrd-exact-ocr-retirement-plan', 'generation': cleanup.GENERATION,
                                    'updates': {'ids': before}})
        self.consumer = cleanup.encode({'kind': 'ccrd-ocr-consumer', 'status': 'consumer-withdrawn', 'resources': []})
        self.reader = Reader({cleanup.IDS: self.before, cleanup.PLAN: self.plan, cleanup.CONSUMER: self.consumer})
        self.writer = Writer(self.reader)
        self.patches = [patch.object(cleanup, 'BEFORE_SHA', hashlib.sha256(self.before).hexdigest()),
                        patch.object(cleanup, 'AFTER_SHA', hashlib.sha256(self.after).hexdigest()),
                        patch.object(cleanup, 'PLAN_SHA', hashlib.sha256(self.plan).hexdigest()),
                        patch.object(cleanup, 'CONSUMER_SHA', hashlib.sha256(self.consumer).hexdigest()),
                        patch.object(cleanup.preflight, '_identity')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def clear(self):
        return cleanup.clear(self.reader, self.writer, Mock())

    def test_exact_withdrawn_ids_are_cleared_only_after_backup_and_intent(self):
        result = self.clear()
        self.assertEqual(result['removed_ids'], 781)
        self.assertEqual(self.reader.objects[cleanup.IDS], self.after)
        self.assertEqual(self.reader.objects[cleanup.SNAPSHOT], self.before)
        self.assertEqual([path for path, _ in self.writer.calls],
                         [cleanup.SNAPSHOT, cleanup.INTENT, cleanup.IDS, cleanup.RECEIPT])
        self.assertEqual(self.writer.calls[2][1], '"etag"')
        self.assertEqual(result['ocr_data_deleted'], 0)
        self.assertFalse(result['producer_restore_authorized'])

    def test_lost_response_reconciles_and_does_not_repeat_registry_write(self):
        self.writer.lose = cleanup.IDS
        with self.assertRaises(cleanup.Unknown):
            self.clear()
        self.assertEqual(self.reader.objects[cleanup.IDS], self.after)
        self.assertEqual(self.clear()['remaining_ids'], 0)
        self.assertEqual(sum(path == cleanup.IDS for path, _ in self.writer.calls), 1)

    def test_active_pointer_or_different_bytes_stop(self):
        self.reader.objects[cleanup.CURRENT] = b'active'
        with self.assertRaisesRegex(ValueError, 'active again'):
            self.clear()
        self.assertFalse(self.writer.calls)
        del self.reader.objects[cleanup.CURRENT]
        self.reader.objects[cleanup.IDS] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'neither reviewed'):
            self.clear()
        self.assertFalse(self.writer.calls)

    def test_existing_foreign_audit_or_missing_saved_before_stops(self):
        self.reader.objects[cleanup.SNAPSHOT] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'audit key differs'):
            self.clear()
        self.assertFalse(self.writer.calls)
        del self.reader.objects[cleanup.SNAPSHOT]
        self.reader.objects[cleanup.IDS] = self.after
        with self.assertRaisesRegex(ValueError, 'lacks its saved before'):
            self.clear()
        self.assertFalse(self.writer.calls)

    def test_plan_mapping_mismatch_is_rejected_even_with_matching_sha(self):
        plan = json.loads(self.plan)
        plan['updates']['ids']['ids'].pop(next(iter(plan['updates']['ids']['ids'])))
        changed = cleanup.encode(plan)
        with patch.object(cleanup, 'PLAN_SHA', hashlib.sha256(changed).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'ownership differs'):
                cleanup.inspect(self.before, changed, self.consumer)


class WriterTests(unittest.TestCase):
    def test_fixed_writer_disables_retries_and_limits_paths(self):
        with patch.dict('os.environ', {'HF_S3_ACCESS_KEY_ID': 'test', 'HF_S3_SECRET_ACCESS_KEY': 'test'}), \
             patch.object(cleanup.boto3, 'client') as factory:
            writer = cleanup.Writer()
        self.assertEqual(factory.call_args.kwargs['config'].retries['total_max_attempts'], 1)
        with self.assertRaises(ValueError):
            writer.put('control/other.json', b'{}')
        with self.assertRaises(ValueError):
            writer.put(cleanup.IDS, b'{}', if_match='etag')
        writer.client.put_object.assert_not_called()

    def test_retired_entrypoints_are_absent_and_historical_identity_remains(self):
        from pathlib import Path
        folder = Path(__file__).resolve().parents[1] / '.github/workflows'
        outer, inner = 'clear-withdrawn-ocr-ids.yml', 'clear-withdrawn-ocr-ids-inner.yml'
        self.assertFalse((folder / outer).exists())
        self.assertFalse((folder / inner).exists())
        self.assertEqual(cleanup.WORKFLOW_REF, f'anftm/pipeline/.github/workflows/{outer}@refs/heads/main')
        for workflow in folder.iterdir():
            if workflow.suffix not in {'.yml', '.yaml'}:
                continue
            with self.subTest(workflow=workflow.name):
                content = workflow.read_text(encoding='utf-8')
                self.assertNotIn(outer, content)
                self.assertNotIn(inner, content)
                self.assertNotIn('clear_withdrawn_ocr_ids.py', content)

    def test_retired_script_still_rejects_untrusted_workflow_context(self):
        for ref in ('', 'anftm/pipeline/.github/workflows/other.yml@refs/heads/main'):
            env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
                   'GITHUB_WORKFLOW_REF': ref, 'GITHUB_RUN_ID': '123'}
            with self.subTest(ref=ref), patch.dict('os.environ', env, clear=True), \
                    patch.object(cleanup, 'Writer') as writer:
                with self.assertRaisesRegex(RuntimeError, 'requires its central workflow'):
                    cleanup.main()
                writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
