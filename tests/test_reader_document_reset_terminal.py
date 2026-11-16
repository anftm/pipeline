import copy
import unittest
from unittest.mock import Mock, patch
from scripts import reader_document_reset_terminal as terminal, reader_gc_documents as collector


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.value={'version':1,'kind':'reader-document-reset-collection','scope':'document-pages',
            'reset_id':'document-rebuild-20261025','bucket':terminal.reset.PAGES,'complete':True,'applied':True,
            'planned_objects':501,'accepted_objects':501,'confirmed_deleted_objects':501,
            'planned_bytes':1000,'confirmed_deleted_bytes':1000,'remaining_objects':4,'retained_objects':4,
            'plan_sha256':'a'*64,'accepted_batches':['b'*64,'c'*64]}
        self.store=Mock();self.store.read_bytes.side_effect=lambda *a:terminal.reset.encode(self.value)

    def read(self):return terminal.completed(self.store,'document-rebuild-20261025','document-pages')

    def test_terminal_requires_no_old_targets_or_inventory(self):
        self.assertEqual(self.read(),self.value);self.store.read_bytes.assert_called_once()
        self.assertTrue(self.store.read_bytes.call_args.args[1].endswith('/collection.json'))
        self.store.list_files.assert_not_called();self.store.delete.assert_not_called()

    def test_invalid_complete_receipt_never_falls_back_to_inventory(self):
        for changes in [{'scope':'ocr-inputs'},{'accepted_objects':500},{'accepted_batches':['b'*64]},
                        {'accepted_batches':['b'*64,'b'*64]},{'confirmed_deleted_bytes':999},
                        {'planned_objects':True},{'bucket':'other/bucket'},{'applied':False}]:
            with self.subTest(changes=changes):
                original=self.value;self.value={**original,**changes}
                with self.assertRaises(ValueError):self.read()
                self.value=original

    def test_missing_or_incomplete_can_use_existing_resume_path(self):
        self.value['complete']=False;self.assertIsNone(self.read())
        self.store.read_bytes.side_effect=FileNotFoundError();self.assertIsNone(self.read())
        self.store.read_bytes.side_effect=OSError('unknown')
        with self.assertRaises(OSError):self.read()

    def test_cli_returns_before_fence_or_scope_inventory(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'result.json'
            with patch('sys.argv',['collector','--scope','document-pages','--reset-id','document-rebuild-20261025','--output',str(output)]), \
                    patch.object(collector,'Store',return_value=self.store), \
                    patch.object(collector.reset,'DocumentProducerFence') as fence, \
                    patch.object(collector,'inventory_scope') as inventory:
                collector.main()
            fence.assert_not_called();inventory.assert_not_called()
            self.assertTrue(terminal.reset.decode(output.read_bytes())['already_complete'])
