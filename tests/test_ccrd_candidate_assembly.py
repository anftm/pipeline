"""All four sources and every metadata/archive identity survive candidate assembly."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import ccrd_candidate_assembly as assembly
from scripts import ccrd_cw_candidate
from scripts import ccrd_metadata_patch as metadata
from scripts import ccrd_source_override as source
from tests.test_ccrd_candidate_preflight import REV, URL, identity
from tests.test_ccrd_cw_candidate import NEW as DB_NEW
from tests.test_ccrd_cw_candidate import OLD as DB_OLD
from tests.test_ccrd_cw_candidate import make_database
from tests.test_ccrd_lifecycle import CcrdLifecycleTests
from tests.test_ccrd_metadata_patch import NEW, OLD, fixture, save


class CandidateAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.parent = self.root / 'parent'; self.parent.mkdir()
        self.output = self.root / 'metadata-candidate'; self.source = self.root / 'source.sqlite'; self.candidate = self.root / 'candidate.sqlite'
        for name, value in fixture().items(): save(self.parent / name, value)
        self.expected = {name: metadata.identity(self.parent / name) for name in metadata.PATHS}
        f = CcrdLifecycleTests('test_four_source_pointer_retains_all_databases_metadata_and_txt_archives'); f.setUp()
        self.manifest = f.four_source_pointer()
        old_prefix = f"generations/{self.manifest['generation']}/"
        self.manifest['generation'] = 'ccrd-four-' + 'd' * 64
        prefix = f"generations/{self.manifest['generation']}/"
        for entry in self.manifest['archives']:
            entry['path'] = entry['path'].replace(old_prefix, prefix, 1)
        for name in sorted(assembly.transport.METADATA_FILES - set(metadata.PATHS)):
            path = self.parent / name; path.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith('.tsv.gz'):
                import gzip
                path.write_bytes(gzip.compress(b'PLA:1\tfixture\n', mtime=0))
            else:
                save(path, {'fixture': name})
            self.expected[name] = metadata.identity(path)
        self.manifest['metadata'] = [{'bucket': 'vomebook/ccrd-index', 'path': prefix + name, 'role': 'runtime',
                                      **self.expected[name]} for name in sorted(self.expected)]
        self.manifest['base_metadata_sha256'] = self.expected[metadata.PATHS[0]]['sha256']
        self.manifest['source_overrides'] = {str(source.CORPUS_PATH): identity(OLD.encode())}
        self.source.write_bytes(b'placeholder source database')
        self.manifest['databases']['CW'].update({'bytes': self.source.stat().st_size,
                                                  'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                                                  'documents': 3, 'fts_rows': 3})
        self.pointer = json.dumps(self.manifest, ensure_ascii=False).encode(); self.pointer_sha = hashlib.sha256(self.pointer).hexdigest()
        self.observed = {'generation': self.manifest['generation'], 'cw0': identity(OLD.encode()),
                         'cw0_bytes_verified': True, 'status_observed_twice': True,
                         'loaded_sources': ['CCRD', 'CW', 'RMRB', 'PLA']}
        self.source_observation = {'url': URL, 'revision': REV, **identity(NEW.encode()), 'source_stable': True}
        expected_patch = {name: self.expected[name] for name in metadata.PATHS}
        metadata.build(self.parent, self.output, expected=expected_patch, old_text=OLD, new_text=NEW, manifest=self.manifest)
        self.candidate.write_bytes(self.source.read_bytes())

    def make(self):
        return assembly.assemble(self.pointer, OLD.encode(), NEW.encode(), self.observed, self.source_observation,
                                 approved_pointer_sha256=self.pointer_sha, parent_metadata=self.parent,
                                 candidate_metadata=self.output, parent_cw=self.source, candidate_cw=self.candidate)

    def test_all_sources_and_references_preserved_in_nonpublishable_description(self):
        with patch.object(assembly, '_cw_database', return_value=({'sha256': 'c' * 64, 'bytes': 42}, {'seconds': 0.1})):
            result = self.make()
            again = self.make()
        self.assertEqual(set(result['preserved_databases']), {'CCRD', 'RMRB', 'PLA'})
        self.assertEqual(len(result['metadata']), 17)
        self.assertEqual([name for name, ref in result['metadata'].items() if ref['source'] == 'candidate'], sorted(metadata.PATHS))
        self.assertEqual(result['archives'], self.manifest['archives'])
        self.assertEqual(result['old_text'], identity(OLD.encode()))
        self.assertEqual(result['new_text'], identity(NEW.encode()))
        self.assertEqual(result['candidate_identity'], again['candidate_identity'])
        self.assertFalse(result['publication_ready']); self.assertEqual(result['remote_writes'], 0)

    def test_wrong_parent_or_missing_metadata_never_yields_candidate(self):
        with self.assertRaisesRegex(ValueError, 'parent pointer identity'):
            assembly.assemble(self.pointer, OLD.encode(), NEW.encode(), self.observed, self.source_observation,
                              approved_pointer_sha256='0' * 64, parent_metadata=self.parent,
                              candidate_metadata=self.output, parent_cw=self.source, candidate_cw=self.candidate)
        (self.parent / 'data/sources.json.gz').unlink()
        with self.assertRaises(ValueError): self.make()

    def test_corrupt_changed_metadata_is_rejected(self):
        path = self.output / metadata.PATHS[3]
        value = json.loads(path.read_bytes()); value['files'][0]['size'] += 1; save(path, value)
        with self.assertRaises(ValueError): self.make()

    def test_real_sqlite_candidate_is_audited_without_mock(self):
        self.source.unlink(); make_database(self.source)
        old_file, new_file = self.root / 'old.txt', self.root / 'new.txt'
        old_file.write_text(DB_OLD); new_file.write_text(DB_NEW)
        output = self.root / 'new-db'
        ccrd_cw_candidate.build(self.source, old_file, new_file, output,
                                source_identity=identity(self.source.read_bytes()),
                                old_identity=identity(old_file.read_bytes()), new_identity=identity(new_file.read_bytes()),
                                expected_documents=3)
        self.manifest['databases']['CW'].update({'bytes': self.source.stat().st_size,
                                                  'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest()})
        result, audit = assembly._cw_database(self.manifest, self.source, output / 'CW.sqlite3', DB_OLD, DB_NEW)
        self.assertEqual(result['sha256'], hashlib.sha256((output / 'CW.sqlite3').read_bytes()).hexdigest())
        self.assertEqual(audit['non_target_rows']['documents'], 2)
        self.assertEqual(audit['sqlite_integrity'], 'ok')

    def test_missing_or_unknown_metadata_member_is_rejected(self):
        value = copy.deepcopy(self.manifest); value['metadata'].pop()
        with self.assertRaisesRegex(ValueError, 'exactly the supported'):
            assembly._metadata_parent(value)


if __name__ == '__main__':
    unittest.main()
