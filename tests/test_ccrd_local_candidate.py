"""A local candidate keeps production untouched and checks all fixture rows."""
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import ccrd_local_candidate as subject
from scripts import ccrd_metadata_patch as metadata
from tests.test_ccrd_candidate_assembly import CandidateAssemblyTests
from tests.test_ccrd_cw_candidate import make_database, NEW as DB_NEW, OLD as DB_OLD
from tests.test_ccrd_metadata_patch import NEW, OLD, fixture, save
from tests.test_ccrd_candidate_preflight import REV, URL, identity


class CcrdLocalCandidateTests(CandidateAssemblyTests):
    def run_build(self, output=None, *, old=OLD, new=NEW):
        return subject.build(self.pointer, old.encode(), new.encode(), consumer=self.observed,
                             source_observation=self.source_observation, parent_metadata=self.parent,
                             parent_cw=self.source, output=output or self.root / 'local-build')

    def real_database(self):
        self.source.unlink(); make_database(self.source)
        self.manifest['databases']['CW'].update({'bytes': self.source.stat().st_size,
                                                 'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest()})
        self.manifest['source_overrides'][str(subject.assembly.preflight.source.CORPUS_PATH)] = identity(DB_OLD.encode())
        values = fixture()
        for relative in metadata.PATHS:
            encoded = json.dumps(values[relative], ensure_ascii=False).replace(OLD, DB_OLD)
            value = json.loads(encoded)
            # Size and snippet fields are updated through the same parser rules.
            def replace_sizes(node):
                if isinstance(node, dict):
                    if node.get('doc_id') == 'CW:0':
                        for key in ('size', 'Size'):
                            if key in node: node[key] = len(DB_OLD.encode())
                        if 'snippet' in node: node['snippet'] = metadata.features.summarize(DB_OLD)
                    for child in node.values(): replace_sizes(child)
                elif isinstance(node, list):
                    for child in node: replace_sizes(child)
            replace_sizes(value)
            save(self.parent / relative, value)
            item = next(row for row in self.manifest['metadata'] if row['path'].endswith('/' + relative))
            item.update(metadata.identity(self.parent / relative))
        self.manifest['base_metadata_sha256'] = metadata.identity(self.parent / metadata.PATHS[0])['sha256']
        self.observed = {**self.observed, 'cw0': identity(DB_OLD.encode())}
        self.source_observation = {'url': URL, 'revision': REV, **identity(DB_NEW.encode()), 'source_stable': True}
        self.pointer = json.dumps(self.manifest, ensure_ascii=False).encode()
        self.pointer_sha = hashlib.sha256(self.pointer).hexdigest()

    def test_real_fixture_build_creates_verified_candidate_only(self):
        self.real_database()
        original = self.source.read_bytes()
        result = self.run_build(old=DB_OLD, new=DB_NEW)
        root = self.root / 'local-build'
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(result['metadata_members'], 17)
        self.assertEqual(result['archive_members'], len(self.manifest['archives']))
        self.assertEqual(result['remote_writes'], 0)
        self.assertFalse(result['pointer_written']); self.assertFalse(result['consumer_switched'])
        self.assertFalse(result['publication_ready']); self.assertFalse(result['external_writers_verified'])
        description = json.loads((root / 'description.json').read_bytes())
        staging = json.loads((root / 'staging-plan.json').read_bytes())
        self.assertEqual(staging['generation'], result['generation'])
        self.assertEqual(description['candidate_identity'], result['candidate_description_sha256'])
        self.assertEqual(set(staging['manifest']['databases']), {'CCRD', 'CW', 'RMRB', 'PLA'})
        self.assertTrue((root / 'cw/CW.sqlite3').is_file())
        self.assertEqual(description['non_target_database_audit']['non_target_rows']['documents'], 2)

    def test_invalid_source_or_consumer_fails_without_output(self):
        output = self.root / 'local-build'
        with self.assertRaisesRegex(ValueError, 'source has not changed'):
            subject.build(self.pointer, OLD.encode(), OLD.encode(), consumer=self.observed,
                          source_observation=self.source_observation, parent_metadata=self.parent,
                          parent_cw=self.source, output=output)
        self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, 'consumer'):
            subject.build(self.pointer, OLD.encode(), NEW.encode(), consumer={**self.observed, 'cw0_bytes_verified': False},
                          source_observation=self.source_observation, parent_metadata=self.parent,
                          parent_cw=self.source, output=output)
        self.assertFalse(output.exists())

    def test_owned_output_cleaned_on_failure_and_inputs_preserved(self):
        output = self.root / 'local-build'; before = self.source.read_bytes()
        with patch.object(subject.cw, 'build', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.run_build(output)
        self.assertFalse(output.exists()); self.assertEqual(self.source.read_bytes(), before)
        output.mkdir(); (output / 'keep').write_text('protected')
        with self.assertRaisesRegex(ValueError, 'new path'):
            self.run_build(output)
        self.assertEqual((output / 'keep').read_text(), 'protected')

    def test_output_cannot_nest_inside_parent_metadata(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            self.run_build(self.parent / 'bad')
        self.assertFalse((self.parent / 'bad').exists())


if __name__ == '__main__':
    unittest.main()
