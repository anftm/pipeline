import json
import hashlib
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from scripts import pilot_static_pdf_stream as stream


class StaticPdfCheckpointTests(unittest.TestCase):
    def test_prior_failure_does_not_replace_an_existing_good_same_revision_mapping(self):
        from scripts.reader_stream_index import merge_indexes
        good={'key':'repo\0book.ps','source_revision':'same','checkpointed_at':'2026-01-01T00:00:00+00:00',
              'object':'documents/pdf/ps/'+'a'*64+'/document.pdf'}
        failure={'key':good['key'],'source_revision':'same','checkpointed_at':'2026-01-02T00:00:00+00:00',
                 'error':'conversion failed'}
        result=merge_indexes({'files':[good],'failures':[]},[],[failure],'static-pdf-stream-index')
        self.assertEqual(result['files'],[good])
        self.assertEqual(result['failures'],[])

    def test_existing_index_only_uses_empty_state_on_confirmed_missing(self):
        from scripts import reader_stream_index
        fs=unittest.mock.Mock()
        with patch.object(reader_stream_index,'HfFileSystem',return_value=fs):
            fs.open.side_effect=FileNotFoundError('missing')
            self.assertEqual(stream.existing_index(stream.BUCKET,None,'documents/pdf/ps')['files'],[])
            for failure in (PermissionError('denied'),OSError('transport')):
                fs.open.side_effect=failure
                with self.assertRaises(type(failure)):
                    stream.existing_index(stream.BUCKET,None,'documents/pdf/ps')
            fs.open.side_effect=None
            for raw in (b'not-json',b'[]',b'{"files":{}}'):
                fs.open.return_value=io.BytesIO(raw)
                with self.assertRaises(ValueError):
                    stream.existing_index(stream.BUCKET,None,'documents/pdf/ps')
    def test_converted_pdf_checkpoint_never_overwrites_a_different_existing_object(self):
        source={'repo':'repo','path':'book.ps','extension':'ps','revision':'new'}
        key='repo\0book.ps'; entry={'key':key,'source_revision':'new','extension':'ps',
            'object':'documents/pdf/ps/'+'a'*64+'/document.pdf',
            'manifest':'documents/pdf/ps/'+'a'*64+'/document-manifest.json'}
        previous={'files':[],'failures':[]};writes=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'commits.json').write_text('{}')
            doc=root/'document.pdf';doc.write_bytes(b'%PDF-new')
            manifest=root/'document-manifest.json';manifest.write_bytes(b'new-manifest')
            args=SimpleNamespace(limit=0,extension='ps',bucket=stream.BUCKET,apply=True,retry_failures=False,
                strict=True,revisions=root/'commits.json',search_data=root/'search.json')
            with patch.object(stream,'parse_args',return_value=args),patch.object(stream,'records',return_value=[source]), \
                 patch.object(stream,'existing_index',return_value=previous), \
                 patch.object(stream,'build',return_value=(entry,{entry['object']:str(doc),entry['manifest']:str(manifest)})), \
                 patch('scripts.reader_stream_index.remote_identity',return_value=('f'*64,999)), \
                 patch('scripts.reader_stream_index.batch_bucket_files_with_retry',side_effect=lambda *a,**k:writes.append((a,k))):
                with self.assertRaisesRegex(ValueError,'different bytes'):
                    stream.main()
            remote_paths=[remote for args,kwargs in writes for local,remote in (kwargs.get('add') or (args[1] if len(args)>1 else []))]
            self.assertNotIn(entry['object'],remote_paths)
            self.assertNotIn('documents/pdf/ps/index.json',remote_paths)

    def test_immutable_pdf_and_manifest_are_verified_before_index_checkpoint(self):
        source={'repo':'repo','path':'book.ps','extension':'ps','revision':'new'}
        entry={'key':'repo\0book.ps','source_revision':'new','extension':'ps',
            'object':'documents/pdf/ps/'+'a'*64+'/document.pdf',
            'manifest':'documents/pdf/ps/'+'a'*64+'/document-manifest.json'}
        remote={};events=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'commits.json').write_text('{}')
            doc=root/'document.pdf';doc.write_bytes(b'%PDF-new')
            manifest=root/'document-manifest.json';manifest.write_bytes(b'new-manifest')
            args=SimpleNamespace(limit=0,extension='ps',bucket=stream.BUCKET,apply=True,retry_failures=False,
                strict=True,revisions=root/'commits.json',search_data=root/'search.json')
            def upload(bucket,files,token,**kwargs):
                for local,path in files:
                    remote[path]=Path(local).read_bytes();events.append(path)
            def identity(fs,bucket,path):
                raw=remote.get(path)
                return None if raw is None else (hashlib.sha256(raw).hexdigest(),len(raw))
            with patch.object(stream,'parse_args',return_value=args),patch.object(stream,'records',return_value=[source]), \
                 patch.object(stream,'existing_index',return_value={'files':[],'failures':[]}), \
                 patch.object(stream,'build',return_value=(entry,{entry['object']:str(doc),entry['manifest']:str(manifest)})), \
                 patch('scripts.reader_stream_index.remote_identity',side_effect=identity), \
                 patch('scripts.reader_stream_index.batch_bucket_files_with_retry',side_effect=upload), \
                 patch('scripts.reader_stream_index.read_index',side_effect=lambda b,p,t,k:json.loads(remote[p])), \
                 patch('scripts.reader_stream_index.HfFileSystem'):
                self.assertEqual(stream.main(),0)
        index='documents/pdf/ps/index.json'
        self.assertTrue(events[0].startswith('reader-index/processing/'))
        self.assertLess(events.index(entry['object']),events.index(index))
        self.assertLess(events.index(entry['manifest']),events.index(index))
        self.assertEqual(remote[entry['object']],b'%PDF-new')
        self.assertTrue(json.loads(remote[index])['files'][0]['processing_roots'])

    def test_distinct_ps_sources_checkpoint_separately_under_bounded_protection(self):
        sources=[{'repo':'repo','path':f'{n}.ps','extension':'ps','revision':'new'} for n in (1,2)]
        checkpoints=[]; previous={'files':[],'failures':[]}; saved={}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'commits.json').write_text('{}')
            args=SimpleNamespace(limit=0,extension='ps',bucket=stream.BUCKET,apply=True,retry_failures=False,
                strict=True,revisions=root/'commits.json',search_data=root/'search.json')
            def build(item,work,token):
                digest=item['path'][0]*64
                path=f'documents/pdf/ps/{digest}/document.pdf'
                manifest=f'documents/pdf/ps/{digest}/document-manifest.json'
                file=root/(item['path']+'.pdf');file.write_bytes(b'%PDF-'+item['path'].encode())
                metadata=root/(item['path']+'.json');metadata.write_bytes(b'manifest')
                return {'key':'repo\0'+item['path'],'source_revision':'new','extension':'ps',
                        'object':path,'manifest':manifest}, {path:str(file),manifest:str(metadata)}
            def checkpoint(bucket,path,old,entries,failures,uploads,work,token,*,apply,kind):
                from scripts.reader_stream_index import merge_indexes
                result=merge_indexes(old,entries,failures,kind)
                checkpoints.append({'entries':list(entries),'uploads':dict(uploads),'result':result})
                saved[path]=result
                return result
            def existing(bucket,token,category):
                return saved.get(category+'/index.json',previous)
            with patch.object(stream,'parse_args',return_value=args),patch.object(stream,'records',return_value=sources), \
                 patch.object(stream,'existing_index',side_effect=existing),patch.object(stream,'build',side_effect=build), \
                 patch.object(stream,'publish_checkpoint',side_effect=checkpoint):
                self.assertEqual(stream.main(),0)
        self.assertEqual(len(checkpoints),2)
        self.assertTrue(all(len(x['entries'])==1 and len(x['uploads'])==2 for x in checkpoints))
        self.assertEqual(len(checkpoints[-1]['result']['files']),2)

    def test_shared_root_with_conflicting_conversion_bytes_fails_before_any_checkpoint(self):
        sources=[{'repo':'repo','path':f'{n}.ps','extension':'ps','revision':'new'} for n in (1,2)]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'commits.json').write_text('{}')
            args=SimpleNamespace(limit=0,extension='ps',bucket=stream.BUCKET,apply=True,retry_failures=False,
                strict=True,revisions=root/'commits.json',search_data=root/'search.json')
            path='documents/pdf/ps/'+'a'*64+'/document.pdf';manifest=path.replace('document.pdf','document-manifest.json')
            def build(item,work,token):
                file=root/(item['path']+'.pdf');file.write_bytes(item['path'].encode())
                return {'key':'repo\0'+item['path'],'source_revision':'new','extension':'ps','object':path,
                        'manifest':manifest},{path:str(file),manifest:str(file)}
            with patch.object(stream,'parse_args',return_value=args),patch.object(stream,'records',return_value=sources), \
                 patch.object(stream,'existing_index',return_value={'files':[],'failures':[]}), \
                 patch.object(stream,'build',side_effect=build),patch.object(stream,'publish_checkpoint') as publish:
                with self.assertRaisesRegex(ValueError,'conflicting static PDF outputs'):
                    stream.main()
                publish.assert_not_called()

    def test_stale_conversion_never_regresses_newer_index_or_uploads_old_bytes(self):
        source={'repo':'repo','path':'book.ps','extension':'ps','revision':'old'}
        key='repo\0book.ps';old={'files':[],'failures':[]}
        newer={'files':[{'key':key,'source_revision':'new','checkpointed_at':'9999-01-01T00:00:00+00:00'}],
               'failures':[]}
        calls=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'commits.json').write_text('{}')
            pdf=root/'document.pdf';pdf.write_bytes(b'%PDF')
            path='documents/pdf/ps/'+'a'*64+'/document.pdf'
            manifest='documents/pdf/ps/'+'a'*64+'/document-manifest.json'
            entry={'key':key,'source_revision':'old','extension':'ps','object':path,'manifest':manifest}
            args=SimpleNamespace(limit=0,extension='ps',bucket=stream.BUCKET,apply=True,retry_failures=False,
                strict=True,revisions=root/'commits.json',search_data=root/'search.json')
            with patch.object(stream,'parse_args',return_value=args),patch.object(stream,'records',return_value=[source]), \
                 patch.object(stream,'existing_index',side_effect=[old,newer]), \
                 patch.object(stream,'build',return_value=(entry,{path:str(pdf),manifest:str(pdf)})), \
                 patch.object(stream,'publish_checkpoint',side_effect=lambda *a,**k:calls.append(a)):
                self.assertEqual(stream.main(),0)
        self.assertEqual(calls,[])

    def test_stale_conversion_cannot_advance_later_failure_or_same_key_checkpoint(self):
        for field in ('files','failures'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'commits.json').write_text('{}')
                source={'repo':'repo','path':'book.ps','extension':'ps','revision':'old'}
                key='repo\0book.ps';path='documents/pdf/ps/'+'a'*64+'/document.pdf'
                manifest=path.replace('document.pdf','document-manifest.json');payload=root/'pdf';payload.write_bytes(b'pdf')
                entry={'key':key,'source_revision':'old','extension':'ps','object':path,'manifest':manifest}
                newer={'files':[],'failures':[]};newer[field]=[{'key':key,'source_revision':'new',
                    'checkpointed_at':'2026-10-28T12:00:00+00:00'}]
                class Clock:
                    @staticmethod
                    def now(tz):return datetime(2026,10,28,10,tzinfo=timezone.utc)
                args=SimpleNamespace(limit=0,extension='ps',bucket=stream.BUCKET,apply=True,retry_failures=False,
                    strict=True,revisions=root/'commits.json',search_data=root/'search.json')
                with patch.object(stream,'datetime',Clock),patch.object(stream,'parse_args',return_value=args), \
                     patch.object(stream,'records',return_value=[source]), \
                     patch.object(stream,'existing_index',side_effect=[{'files':[],'failures':[]},newer]), \
                     patch.object(stream,'build',return_value=(entry,{path:str(payload),manifest:str(payload)})), \
                     patch.object(stream,'publish_checkpoint') as checkpoint:
                    self.assertEqual(stream.main(),0)
                    checkpoint.assert_not_called()

    def test_static_pdf_checkpoint_is_serialized_with_sidecar_writer(self):
        workflow=yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/reader-static-pdf-stream.yml').read_text())
        self.assertEqual(workflow['jobs']['stream']['concurrency']['group'],'reader-sidecar')
        self.assertEqual(workflow['jobs']['finalize']['concurrency']['group'],'reader-sidecar')
        self.assertFalse(workflow['jobs']['stream']['concurrency']['cancel-in-progress'])

    def test_static_pdf_processing_root_is_per_source_and_handed_off(self):
        from scripts import reader_stream_index, reader_lifecycle, build_reader_assets_v2_sidecar as sidecar
        root='documents/pdf/ps/'+'a'*64
        entry={'key':'repo\0book.ps','source_revision':'rev','object':root+'/document.pdf',
               'manifest':root+'/document-manifest.json'}
        with tempfile.TemporaryDirectory() as directory:
            payload=reader_stream_index.publish_checkpoint(stream.BUCKET,'documents/pdf/ps/index.json',
                {'files':[]},[entry],[],{},Path(directory),None,apply=False,kind='static-pdf-stream-index')
            owner=payload['files'][0]['processing_roots'][0]
            record=reader_lifecycle.processing_record([{'bucket':stream.BUCKET,'root':root}],
                                                       'reader-stream-upload',{})
            writes=[]
            self.assertEqual(sidecar.release_processing_roots([payload],'b'*32,lambda path:record,
                lambda path,value:writes.append((path,value))),1)
            self.assertEqual(next(value for path,value in writes if path==owner)['status'],'released')
            self.assertEqual(next(value for path,value in writes if path.startswith('reader-index/gc/retirements/'))['roots'],
                             [{'bucket':stream.BUCKET,'root':root}])

    def test_strict_failure_checkpoints_successes_and_failures_before_exiting(self):
        source = {"repo": "repo", "extension": "ps", "revision": "revision"}
        old_entry = {"key": "repo\0old.ps", "source_revision": "revision", "extension": "ps"}
        pdf_path='documents/pdf/ps/'+'a'*64+'/document.pdf'
        manifest_path='documents/pdf/ps/'+'a'*64+'/document-manifest.json'
        new_entry = {"key": "repo\0good.ps", "source_revision": "revision", "extension": "ps",
                     'object':pdf_path,'manifest':manifest_path}
        previous = {"files": [old_entry], "failures": []}
        uploaded = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revisions = root / "commits.json"
            revisions.write_text('{}', encoding="utf-8")
            args = SimpleNamespace(limit=0, extension="ps", bucket="bucket", apply=True,
                                   retry_failures=False, strict=True, revisions=revisions,
                                   search_data=root / "search_data.json")
            output = root / "document.pdf"
            output.write_bytes(b"%PDF-fixture")

            def checkpoint(bucket,path,old,entries,failures,uploads,work,token,*,apply,kind):
                from scripts.reader_stream_index import merge_indexes
                for remote,local in uploads.items():uploaded[remote]=Path(local).read_bytes()
                result=merge_indexes(old,entries,failures,kind)
                uploaded[path]=json.dumps(result).encode()
                return result

            with patch.object(stream, "parse_args", return_value=args), \
                    patch.object(stream, "records", return_value=[
                        {**source, "path": "old.ps"}, {**source, "path": "good.ps"},
                        {**source, "path": "bad.ps"}]), \
                    patch.object(stream, "existing_index", return_value=previous), \
                    patch.object(stream, "build", side_effect=[
                        (new_entry, {pdf_path: str(output),manifest_path:str(output)}), RuntimeError("HTTP 429")]) as build, \
                    patch.object(stream, "publish_checkpoint", side_effect=checkpoint):
                self.assertEqual(stream.main(), 1)
            self.assertEqual(build.call_count, 2)
            checkpoint = json.loads(uploaded["documents/pdf/ps/index.json"])
            self.assertEqual({entry["key"] for entry in checkpoint["files"]},
                             {old_entry["key"], new_entry["key"]})
            self.assertEqual([failure["key"] for failure in checkpoint["failures"]], ["repo\0bad.ps"])
            self.assertIn("HTTP 429", checkpoint["failures"][0]["error"])
            self.assertEqual(uploaded[pdf_path], b"%PDF-fixture")

    def test_strict_retry_preserves_completed_files_and_clears_recovered_failures(self):
        source = {"repo": "repo", "path": "retry.ps", "extension": "ps", "revision": "revision"}
        pdf_path='documents/pdf/ps/'+'b'*64+'/document.pdf'
        manifest_path='documents/pdf/ps/'+'b'*64+'/document-manifest.json'
        recovered = {"key": "repo\0retry.ps", "source_revision": "revision", "extension": "ps",
                     'object':pdf_path,'manifest':manifest_path}
        previous = {"files": [{"key": "repo\0old.ps", "source_revision": "revision"}],
                    "failures": [{"key": "repo\0retry.ps", "source_revision": "revision"}]}
        uploaded = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revisions = root / "commits.json"
            revisions.write_text('{}', encoding="utf-8")
            converted=root/'document.pdf';converted.write_bytes(b'%PDF-fixture')
            args = SimpleNamespace(limit=0, extension="ps", bucket="bucket", apply=True,
                                   retry_failures=True, strict=True, revisions=revisions,
                                   search_data=root / "search_data.json")

            def checkpoint(bucket,path,old,entries,failures,uploads,work,token,*,apply,kind):
                from scripts.reader_stream_index import merge_indexes
                result=merge_indexes(old,entries,failures,kind)
                uploaded[path]=json.dumps(result).encode()
                return result

            with patch.object(stream, "parse_args", return_value=args), \
                    patch.object(stream, "records", return_value=[source]), \
                    patch.object(stream, "existing_index", return_value=previous), \
                    patch.object(stream, "build", return_value=(recovered, {pdf_path:str(converted),manifest_path:str(converted)})), \
                    patch.object(stream, "publish_checkpoint", side_effect=checkpoint):
                self.assertEqual(stream.main(), 0)
            checkpoint = json.loads(uploaded["documents/pdf/ps/index.json"])
            self.assertEqual(len(checkpoint["files"]), 2)
            self.assertEqual(checkpoint["failures"], [])

    def test_workflows_use_strict_conversion_and_block_failed_finalization(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        for name, job in (("reader-static-pdf-stream.yml", "stream"), ("reader-image-stream.yml", "build")):
            workflow = yaml.safe_load((workflows / name).read_text(encoding="utf-8"))
            with self.subTest(workflow=name):
                commands = "\n".join(step.get("run", "") for step in workflow["jobs"][job]["steps"])
                self.assertIn("--apply --strict)", commands)
                self.assertIn(f"needs.{job}.result == 'success'", workflow["jobs"]["finalize"]["if"])


if __name__ == "__main__":
    unittest.main()
