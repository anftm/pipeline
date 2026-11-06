"""Run both actual Reader sidecar consumers against the same migration fixture."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import publish_reader_v3 as publication
from scripts import reader_page_migrate as migration, reader_page_audit as audit
from tests.test_reader_page_audit import KEY, NEW
from tests import test_reader_page_migrate as fixtures


class DeployedConsumerContractTests(unittest.TestCase):
    def test_hf_decoder_and_pages_projector_resolve_the_same_new_pdf(self):
        # CI can set these to checkouts of the actual consumer repositories.
        # A different directory name is fine; the two implementations are
        # never copied into Pipeline.
        hf = Path(os.environ.get('HF_SEARCH_CHECKOUT', '/nonexistent'))
        pages = Path(os.environ.get('PAGES_SEARCH_CHECKOUT', '/nonexistent'))
        hf_module = hf / 'backend/reader_assets.py'
        pages_module = pages / 'scripts/data/fetch_reader_assets.mjs'
        if not hf_module.is_file() or not pages_module.is_file():
            self.skipTest('actual consumer checkouts were not supplied')
        fixture = fixtures.PageMigrationTests(methodName='test_prepare_and_apply_update_only_indexes_and_leave_all_files')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = migration.apply(fixture.store, fixture.api, fixture.prepare())
        pointer, catalog = publication.current(fixture.store)
        packed = fixture.store.read_bytes(audit.ASSETS, catalog['base_sidecar']['path'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pointer.json').write_bytes(publication.encode(pointer))
            (root / 'catalog.json').write_bytes(publication.encode(catalog))
            (root / 'base.gz').write_bytes(packed)
            (root / 'key.json').write_text(json.dumps(KEY))
            python = ('import json,sys; from pathlib import Path; '
                      'from backend.reader_assets import decode_reader_assets; '
                      'd=decode_reader_assets(Path(sys.argv[1]).read_bytes()); '
                      'print(json.dumps(d["f"][json.loads(Path(sys.argv[2]).read_text())],ensure_ascii=False))')
            py = subprocess.run([os.environ.get('PYTHON', 'python3'), '-c', python, str(root/'base.gz'), str(root/'key.json')],
                                cwd=hf, capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': str(hf)})
            self.assertEqual(py.returncode, 0, py.stderr)
            self.assertEqual(json.loads(py.stdout)['p'], NEW)
            js = ("const fs=require('fs');(async()=>{const m=await import(process.argv[1]);"
                  "const base=m.validateReaderAssets(fs.readFileSync(process.argv[4]));"
                  "const d=m.projectV3(base,JSON.parse(fs.readFileSync(process.argv[2])),"
                  "fs.readFileSync(process.argv[3]),fs.readFileSync(process.argv[4]));"
                  "console.log(JSON.stringify(d.f[JSON.parse(fs.readFileSync(process.argv[5]))]));})().catch(e=>{console.error(e);process.exit(1)});")
            node = subprocess.run(['node', '-e', js, pages_module.as_uri(), str(root/'pointer.json'),
                                   str(root/'catalog.json'), str(root/'base.gz'), str(root/'key.json')],
                                  capture_output=True, text=True)
            self.assertEqual(node.returncode, 0, node.stderr)
            self.assertEqual(json.loads(node.stdout)['p'], NEW)
        self.assertEqual(pointer['generation'], result['generation'])


if __name__ == '__main__':
    unittest.main()
