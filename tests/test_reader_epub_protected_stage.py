"""Protected stage preserves order and resolves uncertain writes by readback."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import reader_epub_protected_stage as subject, reader_epub_shadow as shadow
from tests.test_reader_epub_shadow import epub, plan

WHEN = '2026-11-09T00:00:00Z'


class MemoryProtections:
    def __init__(self, events):self.values={};self.events=events;self.lose=False;self.fail=False
    def inspect(self,path):self.events.append(('inspect-protection',path));return self.values.get(path)
    def create_once(self,path,raw):
        self.events.append(('create-protection',path))
        if self.fail:raise TimeoutError('before write')
        if path in self.values:raise ValueError('already exists')
        self.values[path]=raw
        if self.lose:raise TimeoutError('lost response')
        return 'written'


class MemoryObjects:
    def __init__(self,events):self.values={};self.events=events;self.lose=False;self.fail=False
    def inspect(self,root,path):
        self.events.append(('inspect-object',root+'/'+path));value=self.values.get(root+'/'+path)
        return None if value is None else {'sha256':hashlib.sha256(value).hexdigest(),'bytes':len(value)}
    def create_once(self,root,path,local,expected):
        key=root+'/'+path;self.events.append(('create-object',key))
        if self.fail:raise TimeoutError('before write')
        if key in self.values:raise ValueError('already exists')
        self.values[key]=local.read_bytes()
        if self.lose:raise TimeoutError('lost response')
        return 'written'


class ProtectedStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.output=Path(self.tmp.name)/'shadow';raw=epub();self.plan=plan(raw)
        self.report=shadow.build(self.plan,self.output,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.output)
        self.events=[];self.protections=MemoryProtections(self.events);self.objects=MemoryObjects(self.events)
        self.validate=Mock()
    def run_stage(self):
        return subject.stage(self.plan,self.report,self.output,created_at=WHEN,protections=self.protections,
                             objects=self.objects,revalidate=self.validate)
    def test_protection_precedes_content_and_explicit_resume_is_read_only(self):
        result=self.run_stage();self.assertEqual(result['protected_sources'],1)
        self.assertGreater(result['verified_objects'],1);self.assertFalse(result['publication_ready'])
        self.assertFalse(result['index_written']);self.assertFalse(result['pointer_written'])
        self.assertFalse(result['external_writers_verified']);self.assertEqual(self.validate.call_count,3)
        writes=[event for event in self.events if event[0].startswith('create-')]
        self.assertEqual(writes[0][0],'create-protection')
        self.assertTrue(all(e[0]=='create-object' for e in writes[1:]))
        count=len(writes);again=self.run_stage()
        self.assertEqual(len([event for event in self.events if event[0].startswith('create-')]),count)
        self.assertTrue(all(row['status']=='already-present' for row in again['outcomes']))
    def test_lost_response_is_read_exactly_without_replay(self):
        self.protections.lose=True;self.objects.lose=True
        result=self.run_stage();self.assertGreater(result['verified_objects'],1)
        creates=[event for event in self.events if event[0].startswith('create-')]
        self.assertEqual(len(creates),len(set(creates)))
        self.assertTrue(any(row['status']=='written-response-unknown' for row in result['outcomes']))
    def test_unknown_before_protection_blocks_all_objects(self):
        self.protections.fail=True
        with self.assertRaisesRegex(subject.UnknownOutcome,'inspect before recovery'):self.run_stage()
        self.assertFalse(self.objects.values)
        self.assertEqual(sum(event[0]=='create-protection' for event in self.events),1)
    def test_conflicts_never_replace_existing_content(self):
        path=self.preview['actions'][0]['protection_path'];self.protections.values[path]=b'foreign'
        with self.assertRaisesRegex(ValueError,'conflicts'):self.run_stage()
        self.assertFalse(self.objects.values)
        self.protections.values.clear()
        item=self.preview['actions'][0]['files'][0];self.objects.values[item['remote']]=b'foreign'
        with self.assertRaisesRegex(ValueError,'conflicts'):self.run_stage()
        self.assertEqual(self.objects.values[item['remote']],b'foreign')
        self.assertNotIn(('create-object',item['remote']),self.events)
    def test_revalidation_and_tampering_fail_closed(self):
        self.validate.side_effect=ValueError('source head changed')
        with self.assertRaisesRegex(ValueError,'source head changed'):self.run_stage()
        self.assertFalse(self.protections.values);self.assertFalse(self.objects.values)
        self.validate.reset_mock();self.validate.side_effect=None
        item=self.preview['actions'][0]['files'][0];p=self.output/'0'/'bundle'/item['path']
        p.write_bytes(p.read_bytes()+b'changed')
        with self.assertRaises(ValueError):self.run_stage()
        self.assertFalse(self.protections.values);self.assertFalse(self.objects.values)
    def test_unknown_object_stays_protected_and_resume_reconciles(self):
        self.objects.fail=True
        with self.assertRaises(subject.UnknownOutcome):self.run_stage()
        self.assertEqual(len(self.protections.values),1);self.assertFalse(self.objects.values)
        self.objects.fail=False;result=self.run_stage()
        self.assertEqual(result['protected_sources'],1);self.assertGreater(result['verified_objects'],1)


if __name__=='__main__':unittest.main()
