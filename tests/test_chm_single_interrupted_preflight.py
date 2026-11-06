import copy
import hashlib
import json
import unittest

from scripts import chm_single_interrupted_preflight as check


class Store:
    def __init__(self, values):
        self.values = values

    def read_bytes(self, bucket, path):
        if bucket != check.ASSETS:
            raise AssertionError('unexpected bucket')
        return self.values[path]


class ChmSingleInterruptedPreflightTests(unittest.TestCase):
    def test_route_diff_requires_only_the_exact_old_and_new_chm(self):
        before = {'v': 1, 'f': {check.SOURCE_KEY: check.route(check.OLD_ROOT), 'other': {'s': 1}}}
        after = {'v': 1, 'f': {check.SOURCE_KEY: check.route(check.ROOT), 'other': {'s': 1}}}
        check.validate_route_change(before, after)
        for mutate in ('other', 'source', 'missing'):
            value = copy.deepcopy(after)
            if mutate == 'other':
                value['f']['other'] = {'s': 2}
            elif mutate == 'source':
                value['f'][check.SOURCE_KEY]['p'] = 'other'
            else:
                del value['f'][check.SOURCE_KEY]
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                check.validate_route_change(before, value)

    def test_processing_record_must_keep_exact_run_and_root(self):
        from unittest.mock import patch
        value = {'version': 1, 'kind': 'reader-processing-roots', 'status': 'processing',
                 'operation': 'reader-stream-upload', 'release_policy': 'explicit-publisher-handoff',
                 'run': {'repository': 'anftm/pipeline', 'run_id': str(check.SOURCE_RUN), 'attempt': '1'},
                 'resources': [{'bucket': check.ASSETS, 'root': check.ROOT}]}
        raw = json.dumps(value).encode()
        with patch.object(check, 'PROCESSING_SHA256', check.digest(raw)):
            check.validate_processing(raw)
            for field in ('status', 'run', 'resources'):
                bad = copy.deepcopy(value)
                bad[field] = None
                with self.subTest(field=field), self.assertRaises(ValueError):
                    check.validate_processing(json.dumps(bad).encode())

    def test_index_requires_exact_owner_and_processing_root(self):
        entry = {'key': check.SOURCE_KEY, 'root': check.ROOT,
                 'manifest': check.ROOT + '/chapter-manifest.json',
                 'source_sha256': check.SOURCE_SHA, 'processing_roots': [check.PROCESSING]}
        self.assertEqual(check.validate_entry({'files': [entry]}), entry)
        for records in ([], [entry, entry], [{**entry, 'processing_roots': []}],
                        [{**entry, 'source_sha256': 'f' * 64}]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                check.validate_entry({'files': records})

    def test_manifest_members_are_verified_before_acceptance(self):
        from unittest.mock import patch
        values = {}
        def item(path, content):
            values[check.ROOT + '/' + path] = content
            return {'path': path, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        manifest = {'kind': 'epub-chapters', 'chapters': [item('chapters/1.xhtml', b'one'),
                                                           item('chapters/2.xhtml', b'two')],
                    'search_index': item('search.json.gz', b'index')}
        raw = json.dumps(manifest).encode()
        values[check.ROOT + '/chapter-manifest.json'] = raw
        with patch.object(check, 'MANIFEST_SHA256', check.digest(raw)):
            check.validate_manifest(Store(values))
            values[check.ROOT + '/chapters/1.xhtml'] = b'bad'
            with self.assertRaisesRegex(ValueError, 'checksum differs'):
                check.validate_manifest(Store(values))

    def test_preflight_exposes_no_apply_or_remote_write_path(self):
        import ast
        import inspect
        source = inspect.getsource(check)
        tree = ast.parse(source)
        prohibited = {'put_bytes', 'put_object', 'delete', 'publish_catalog', 'release_processing',
                      'publish', 'batch_bucket_files', 'update'}
        invoked = {node.func.attr for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse(invoked & prohibited)
        self.assertNotIn('--apply', source)

    def test_source_run_and_stage_identity_are_required(self):
        repo = {'id': 1443726000, 'full_name': 'anftm/pipeline'}
        run = {'id': check.SOURCE_RUN, 'repository': {'id': repo['id']},
               'path': '.github/workflows/reader-chm-repair.yml',
               'head_sha': '82018d6f5f34dcb4f12da21e967679a0a508841d',
               'event': 'workflow_dispatch', 'run_attempt': 1, 'status': 'completed', 'conclusion': 'failure'}
        steps = [{'name': 'Build chapter streams from pinned CHMs (EPUB is temporary)', 'conclusion': 'success'},
                 {'name': 'Rebuild the canonical Reader sidecar and release handed-off roots', 'conclusion': 'failure'},
                 {'name': 'Unify the updated Reader projection', 'conclusion': 'skipped'}]
        jobs = {'total_count': 2, 'jobs': [{'name': 'plan', 'status': 'completed'},
                                           {'name': 'rebuild', 'status': 'completed',
                                            'conclusion': 'failure', 'steps': steps}]}
        def api(path):
            return repo if not path else jobs if '/jobs?' in path else run
        check.validate_github(api)
        for field, value in (('run_attempt', 2), ('status', 'in_progress'), ('conclusion', 'success')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                original = run[field]
                run[field] = value
                try:
                    check.validate_github(api)
                finally:
                    run[field] = original
        jobs['jobs'][1]['steps'][-1]['conclusion'] = 'success'
        with self.assertRaisesRegex(ValueError, 'stages differ'):
            check.validate_github(api)


if __name__ == '__main__':
    unittest.main()
