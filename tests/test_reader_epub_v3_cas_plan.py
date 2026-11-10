"""Exact EPUB v3 pointer preconditions never authorize publication."""
import copy
import hashlib
import json
import unittest
from unittest.mock import Mock,patch

from scripts import reader_epub_v3_cas_plan as subject
from tests.test_reader_epub_v3_preview import V3PreviewTests


class PointerCasPlanTests(unittest.TestCase):
    def setUp(self):
        f=V3PreviewTests(methodName='test_selected_update_keeps_other_routes_and_pdf_redirects')
        f.setUp();self.addCleanup(f.doCleanups);self.f=f
        generation=f.pointer['generation']
        ref={'bucket':subject.BUCKET,'path':f'reader-index/v3/generations/{generation}/catalog.json',
             'sha256':'c'*64,'bytes':42,'role':'runtime'}
        f.pointer.update(version=1,kind='reader-reading-pointer',catalog=ref,resources=[ref])
        self.raw=(json.dumps(f.pointer,sort_keys=True,indent=2)+'\n').encode();self.etag='"pointer-etag"'
        f.store.read_bytes.side_effect=lambda bucket,path:self.raw if (bucket,path)==(subject.BUCKET,subject.POINTER) else b'foreign'
        self.pointer_store=Mock();self.pointer_store.read_for_update.return_value=(self.raw,self.etag)

    def inspect(self):
        return subject.inspect(self.f.store,self.f.f.store,self.f.f.index_store,self.f.f.target,self.f.f.plan['operation'],
            pointer_store=self.pointer_store,checkpoints=self.f.f.checkpoints,protections=self.f.f.protections,
            objects=self.f.f.objects,revalidate=self.f.validator,client=self.f.client)

    def test_exact_parent_raw_etag_candidate_and_history_are_preserved_without_writes(self):
        value,old,etag,new,packed=self.inspect()
        self.assertEqual(old,self.raw);self.assertEqual(etag,self.etag)
        self.assertEqual(value['parent_pointer_raw_sha256'],hashlib.sha256(old).hexdigest())
        self.assertEqual(value['parent_pointer_etag_sha256'],hashlib.sha256(etag.encode()).hexdigest())
        self.assertEqual(value['candidate_pointer_raw_sha256'],hashlib.sha256(new).hexdigest())
        self.assertEqual(value['candidate_base']['sha256'],hashlib.sha256(packed).hexdigest())
        self.assertEqual(value['previous_routes'][self.f.key],self.f.old)
        self.assertEqual(value['preserved_pdf_redirects'],1)
        self.assertFalse(value['pointer_written']);self.assertFalse(value['publication_ready'])
        self.assertFalse(value['rollback_authorized']);self.assertEqual(value['remote_writes'],0)
        self.pointer_store.put_if_match.assert_not_called();self.f.store.put_bytes.assert_not_called()

    def test_parent_validator_or_exact_bytes_drift_stops_even_when_json_is_equal(self):
        altered=json.dumps(self.f.pointer,sort_keys=True).encode()
        self.pointer_store.read_for_update.side_effect=[(self.raw,self.etag),(altered,self.etag)]
        with self.assertRaisesRegex(ValueError,'exact bytes or validator changed'):self.inspect()
        self.pointer_store.read_for_update.side_effect=[(self.raw,self.etag),(self.raw,'"other"')]
        with self.assertRaisesRegex(ValueError,'exact bytes or validator changed'):self.inspect()
        self.pointer_store.put_if_match.assert_not_called()

    def test_hub_and_s3_must_observe_exact_same_parent_bytes(self):
        self.f.store.read_bytes.side_effect=lambda *_:b'not the exact pointer'
        with self.assertRaisesRegex(ValueError,'differs between S3 and Hub'):self.inspect()
        self.pointer_store.put_if_match.assert_not_called()

    def test_second_preview_must_match_and_rechecks_protected_objects(self):
        original=subject.preview.inspect;calls=0
        def change(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:
                action=self.f.f.preview['actions'][0]
                self.f.f.protections.values.pop(action['protection_path'])
            return original(*args,**kwargs)
        with patch.object(subject.preview,'inspect',side_effect=change):
            with self.assertRaisesRegex(ValueError,'protection changed'):self.inspect()
        self.pointer_store.put_if_match.assert_not_called()

    def test_unknown_or_malformed_pointer_never_becomes_plan(self):
        for raw in (b'{"version":1,"version":1,"kind":"reader-reading-pointer"}',b'{}',b'[]',b'{'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):subject._parse_pointer(raw)
        self.pointer_store.read_for_update.return_value=(self.raw,'W/"weak"')
        with self.assertRaisesRegex(ValueError,'strong S3 ETag'):self.inspect()

    def test_pointer_parent_or_preview_tamper_is_rejected(self):
        original=subject.preview.inspect
        def tampered(*args,**kwargs):
            value,packed=original(*args,**kwargs);value=copy.deepcopy(value)
            value['parent_pointer']['generation']='f'*64
            return value,packed
        with patch.object(subject.preview,'inspect',side_effect=tampered):
            with self.assertRaisesRegex(ValueError,'does not bind'):self.inspect()
        self.pointer_store.put_if_match.assert_not_called()


if __name__=='__main__':unittest.main()
