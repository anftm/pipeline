"""Four-source TXT plans must remain offline, narrow and fail closed."""

from copy import deepcopy
import gzip
import hashlib
import json
import unittest

from scripts import ccrd_single_source_plan as plan


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


class SingleSourcePlanTests(unittest.TestCase):
    def setUp(self):
        self.current = '旧的合成文本'.encode()
        self.new = '新的合成文本更长'.encode()
        self.metadata = {
            'version': 1,
            'unrelated': {'preserve': True},
            'sources': [{'slug': source, 'name': source, 'count': count}
                        for source, count in [('CCRD', 1), ('CW', 2), ('RMRB', 1), ('PLA', 1)]],
            'records': [
                {'source': 'CCRD', 'doc_id': 'CCRD:4', 'size': 9},
                {'source': 'CW', 'doc_id': 'CW:0', 'storage_root': 'CW',
                 'storage_rel_path': plan.TARGET_RELATIVE_PATH, 'size': len(self.current), 'marker': 'preserve'},
                {'source': 'CW', 'doc_id': 'CW:1', 'size': 7, 'storage_rel_path': '1/other.txt'},
                {'source': 'RMRB', 'doc_id': 'RMRB:1', 'size': 3},
                {'source': 'PLA', 'doc_id': 'PLA:1', 'size': 5},
            ],
        }
        self.databases = {source: {'documents': count, 'fts_rows': count,
                                  'sha256': hashlib.sha256(source.encode()).hexdigest()}
                          for source, count in [('CCRD', 1), ('CW', 2), ('RMRB', 1), ('PLA', 1)]}
        self.pointer = {'format': 2, 'kind': 'ccrd-four-source-generation',
                        'generation': 'ccrd-four-' + 'a' * 64,
                        'databases': self.databases,
                        'source_overrides': {plan.TARGET_PATH: {'sha256': plan.sha(self.current)}}}

    def arguments(self, metadata=None, pointer=None, activation=None, current=None, new=None):
        metadata = self.metadata if metadata is None else metadata
        pointer = deepcopy(self.pointer if pointer is None else pointer)
        compressed = gzip.compress(encode(metadata), mtime=0)
        pointer['base_metadata_sha256'] = plan.sha(compressed)
        pointer_bytes = encode(pointer)
        if activation is None:
            activation = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': 'vomebook/ccrd-index',
                          'path': f'generations/{pointer["generation"]}/manifest.json',
                          'sha256': plan.sha(pointer_bytes)}
        return {'space_revision': 'b' * 40, 'metadata_gz': compressed, 'pointer_bytes': pointer_bytes,
                'activation_bytes': encode(activation), 'current_txt': self.current if current is None else current,
                'next_txt': self.new if new is None else new}

    def test_changing_txt_only_changes_its_metadata_size_and_preserves_other_sources(self):
        args = self.arguments()
        result = plan.plan_update(**args)
        updated = json.loads(gzip.decompress(result['candidate_metadata_gz']))
        expected = deepcopy(self.metadata)
        expected['records'][1]['size'] = len(self.new)
        self.assertEqual(updated, expected)
        self.assertEqual(result['source_counts'], {'CCRD': 1, 'CW': 2, 'RMRB': 1, 'PLA': 1})
        self.assertEqual(result['unchanged_database_sha256'],
                         {source: self.databases[source]['sha256'] for source in ('CCRD', 'RMRB', 'PLA')})
        self.assertEqual(result['explicit_deletions'], [])
        self.assertEqual(result['remote_writes'], 0)
        self.assertFalse(result['publication_ready'])
        self.assertTrue(result['additional_metadata_review_required'])
        self.assertIn('rebuild-cw-index', result['required_actions'])

    def test_identical_txt_is_noop_even_if_input_gzip_is_not_canonical(self):
        args = self.arguments(new=self.current)
        result = plan.plan_update(**args)
        self.assertFalse(result['content_changed'])
        self.assertEqual(result['required_actions'], [])
        self.assertEqual(result['candidate_metadata_gz'], args['metadata_gz'])
        self.assertFalse(result['publication_ready'])

    def test_same_length_changed_text_still_requires_cw_rebuild(self):
        new = b'x' * len(self.current)
        args = self.arguments(new=new)
        result = plan.plan_update(**args)
        self.assertTrue(result['content_changed'])
        self.assertFalse(result['metadata_semantically_changed'])
        self.assertEqual(result['candidate_metadata_gz'], args['metadata_gz'])
        self.assertIn('rebuild-cw-index', result['required_actions'])
        self.assertTrue(result['additional_metadata_review_required'])

    def test_stale_manifest_or_activation_never_produces_a_plan(self):
        for key, value in [('space_revision', 'invalid'), ('activation_bytes', b'{}'),
                           ('current_txt', b'different existing text'), ('metadata_gz', b'broken'),
                           ('next_txt', b''), ('next_txt', b'\xff')]:
            with self.subTest(key=key, value=value):
                args = self.arguments()
                args[key] = value
                with self.assertRaises(ValueError):
                    plan.plan_update(**args)
        args = self.arguments()
        args['metadata_gz'] = gzip.compress(encode({'records': [], 'sources': []}), mtime=0)
        with self.assertRaisesRegex(ValueError, 'metadata differs'):
            plan.plan_update(**args)

    def test_duplicate_keys_and_nonfinite_values_are_rejected(self):
        args = self.arguments()
        pointer = json.loads(args['pointer_bytes'])
        duplicate_pointer = encode(pointer).replace(b'"format":2', b'"format":2,"format":2')
        activation = json.loads(args['activation_bytes'])
        activation['sha256'] = plan.sha(duplicate_pointer)
        with self.assertRaises(ValueError):
            plan.plan_update(**{**args, 'pointer_bytes': duplicate_pointer, 'activation_bytes': encode(activation)})
        valid_activation = args['activation_bytes']
        duplicate_activation = valid_activation.replace(b'"version":1', b'"version":1,"version":1')
        for key, value in [('activation_bytes', duplicate_activation)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                plan.plan_update(**{**args, key: value})
        for raw in (b'{"records":[],"records":[]}', b'{"value":NaN}'):
            compressed = gzip.compress(raw, mtime=0)
            pointer = json.loads(args['pointer_bytes'])
            pointer['base_metadata_sha256'] = plan.sha(compressed)
            encoded_pointer = encode(pointer)
            activation = json.loads(args['activation_bytes'])
            activation['sha256'] = plan.sha(encoded_pointer)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                plan.plan_update(**{**args, 'metadata_gz': compressed, 'pointer_bytes': encoded_pointer,
                                    'activation_bytes': encode(activation)})

    def test_missing_or_duplicate_records_and_counts_are_rejected(self):
        variants = []
        missing = deepcopy(self.metadata)
        missing['records'][1]['doc_id'] = 'CW:10'
        variants.append(missing)
        duplicate = deepcopy(self.metadata)
        duplicate['records'][2]['doc_id'] = 'CW:0'
        variants.append(duplicate)
        extra = deepcopy(self.metadata)
        extra['records'][0]['source'] = 'new-source'
        variants.append(extra)
        wrong_count = deepcopy(self.metadata)
        wrong_count['sources'][2]['count'] += 1
        variants.append(wrong_count)
        wrong_path = deepcopy(self.metadata)
        wrong_path['records'][1]['storage_rel_path'] = '1/other.txt'
        variants.append(wrong_path)
        wrong_size = deepcopy(self.metadata)
        wrong_size['records'][1]['size'] += 1
        variants.append(wrong_size)
        derived_content = deepcopy(self.metadata)
        derived_content['records'][1]['text_sha256'] = plan.sha(self.current)
        variants.append(derived_content)
        wrong_id_source = deepcopy(self.metadata)
        wrong_id_source['records'][0]['doc_id'] = 'CW:123'
        variants.append(wrong_id_source)
        for item in variants:
            with self.subTest(item=item), self.assertRaises(ValueError):
                plan.plan_update(**self.arguments(metadata=item))

    def test_two_source_pointer_and_changed_database_identity_are_rejected(self):
        old = deepcopy(self.pointer)
        old['format'] = 1
        old['databases'] = {key: self.databases[key] for key in ('CCRD', 'CW')}
        with self.assertRaisesRegex(ValueError, 'four-source'):
            plan.plan_update(**self.arguments(pointer=old))
        bad_db = deepcopy(self.pointer)
        bad_db['databases']['RMRB']['documents'] += 1
        with self.assertRaisesRegex(ValueError, 'database identity'):
            plan.plan_update(**self.arguments(pointer=bad_db))


if __name__ == '__main__':
    unittest.main()
