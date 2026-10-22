"""Tiny real SQLite/export protocol; in-memory remote and Space, never live IO."""
from __future__ import annotations

import copy
import gzip
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from scripts import ccrd_newspaper_transport as transport, ccrd_ocr_release as contract
from scripts import publish_ccrd_newspapers as rollout, publish_ccrd_ocr as ocr
from scripts import pin_ccrd_newspaper_consumer as pin, auto_ccrd_ocr as auto
from scripts import ccrd_lifecycle as lifecycle, reader_gc_ccrd as gc
from scripts.reader_gc_stream import graph_json
from scripts import package_ccrd_newspaper_transport as packager
from scripts import verify_ccrd_newspapers_live as live


URL = 'https://github.com/anftm/pipeline/releases/download/newspaper-fixture/transport.json'
BUCKET = contract.BUCKET
TINY_BUILDER = b'''
from pathlib import Path
import sqlite3, unicodedata
TOKENIZER_VERSION = "cjk-bigram-boundary-fts5-v6-snippet-anchors"
def build_source_db(source, records):
    path = FULLTEXT_DIR / (source + ".sqlite3")
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE documents(doc_id TEXT PRIMARY KEY,summary TEXT);
            CREATE TABLE postings(token_hash BLOB PRIMARY KEY,docs BLOB,doc_count INTEGER);
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE VIRTUAL TABLE content_fts USING fts5(content,tokenize='trigram');
            CREATE TABLE snippet_anchors(token_hash BLOB,doc_number INTEGER,char_offset INTEGER);
        """)
        db.execute("INSERT INTO metadata VALUES('tokenizer_version',?)", (TOKENIZER_VERSION,))
        for record in records:
            body = (BASE_DIR / record["storage_rel_path"]).read_text()
            n = int(record["doc_id"].split(":")[1])
            db.execute("INSERT INTO documents VALUES(?,?)", (record["doc_id"],body[:700]))
            db.execute("INSERT INTO content_fts(rowid,content) VALUES(?,?)",
                (n,unicodedata.normalize("NFKC",body).casefold().replace("\\0"," ")))
'''


class Store:
    def __init__(self):
        self.objects, self.events = {}, []
        self.corrupt_upload = None
    def read_bytes(self, bucket, path):
        try:
            return self.objects[bucket, path]
        except KeyError as error:
            raise FileNotFoundError(path) from error
    def put_bytes(self, bucket, path, raw):
        self.events.append(('put', path))
        self.objects[bucket, path] = raw
    def put_file(self, bucket, path, local):
        self.events.append(('file', path))
        raw = local.read_bytes()
        self.objects[bucket, path] = b'bad' if path == self.corrupt_upload else raw
    def file_identity(self, bucket, path):
        raw = self.read_bytes(bucket, path)
        self.events.append(('full-readback', path))
        return {'sha256': contract.digest(raw), 'bytes': len(raw)}


class Space:
    def __init__(self, files, events):
        self.revision = '1' * 40
        self.snapshots = {self.revision: files}
        self.events = events
        self.fail_commit = False
    def head(self):
        return self.revision
    def read(self, path, revision):
        return self.snapshots[revision][path]
    def commit(self, parent, files):
        if parent != self.revision:
            raise ValueError('stale fixture Space parent')
        if self.fail_commit:
            raise ConnectionError('fixture interrupted Space commit')
        self.events.append(('space-commit', sorted(files)))
        content = {**self.snapshots[parent], **{name: data if isinstance(data, bytes) else data.read_bytes()
                                               for name, data in files.items()}}
        self.revision = '2' * 40
        self.snapshots[self.revision] = content
        return self.revision


class NewspaperPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store()
        self.generation = 'ccrd-four-' + 'a' * 64
        self.prefix = f'generations/{self.generation}/'
        records = [{"source": source, "doc_id": source + ':1'} for source in ['CCRD', 'CW', 'RMRB', 'PLA']]
        old_raw = gzip.compress(contract.canonical({'records': records[:2], 'sources': [
            {'slug': s, 'count': 1} for s in ['CCRD', 'CW']]}), mtime=0)
        self.old_sha = contract.digest(old_raw)
        new_raw = gzip.compress(contract.canonical({'records': records, 'sources': [
            {'slug': s, 'count': 1} for s in ['CCRD', 'CW', 'RMRB', 'PLA']]}), mtime=0)
        databases = {}
        for source in ['CCRD', 'CW']:
            path = f'generations/ccrd-old/{source}.sqlite3'
            raw = b'legacy database ' + source.encode()
            self.store.objects[BUCKET, path] = raw
            databases[source] = {'path': path, 'sha256': contract.digest(raw), 'bytes': len(raw),
                                 'documents': 1, 'fts_rows': 1}
        old_pointer = {'format': 1, 'tokenizer_version': contract.TOKENIZER, 'databases': copy.deepcopy(databases)}
        old_pointer_raw = contract.canonical(old_pointer)
        self.store.objects[BUCKET, 'current.json'] = old_pointer_raw
        for source in ['RMRB', 'PLA']:
            temporary_db = self.root / (source + '.sqlite3')
            with sqlite3.connect(temporary_db) as db:
                db.executescript('''CREATE TABLE documents(doc_id TEXT PRIMARY KEY,summary TEXT);
                    CREATE TABLE texts(doc_id TEXT PRIMARY KEY,body TEXT);
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                    CREATE VIRTUAL TABLE content_fts USING fts5(content,tokenize='trigram');''')
                db.executemany('INSERT INTO metadata VALUES(?,?)', [('build_complete','true'),
                    ('index_profile','newspaper-hybrid-texts-v1'), ('tokenizer_version',contract.TOKENIZER)])
                db.execute('INSERT INTO documents VALUES(?,?)', (source + ':1', '正文'))
                db.execute('INSERT INTO texts VALUES(?,?)', (source + ':1', '正文'))
                db.execute("INSERT INTO content_fts(rowid,content) VALUES(1,'正文')")
            details = transport.identity(temporary_db)
            path = f'indexes/newspapers/{source}/{details["sha256"]}.sqlite3'
            target = self.root / path
            target.parent.mkdir(parents=True)
            temporary_db.replace(target)
            databases[source] = {'path': path, **details, 'source': source, 'documents': 1, 'fts_rows': 1,
                'text_rows': 1, 'index_profile': 'newspaper-hybrid-texts-v1'}
        metadata = [self.file_ref(self.prefix + name, new_raw if name == 'data/search_data.json.gz' else b'{}\n')
                    for name in sorted(transport.METADATA_FILES)]
        archives = [{**self.file_ref(f'{self.prefix}archives/{source}/{source}-1976.tar.gz', b'archive fixture'),
                     'source': source, 'year': 1976, 'documents': 1} for source in ['RMRB', 'PLA']]
        self.manifest = {'format': 2, 'kind': 'ccrd-four-source-generation', 'generation': self.generation,
            'tokenizer_version': contract.TOKENIZER, 'base_metadata_sha256': contract.digest(new_raw),
            'previous_base_metadata_sha256': self.old_sha,
            'previous_index_manifest_sha256': contract.digest(old_pointer_raw),
            'databases': databases, 'metadata': metadata, 'archives': archives,
            'builder': {'sha256': contract.digest(TINY_BUILDER)}}
        self.old_runtime = {'app.py': b'old app', 'prepare_fulltext_index.py': b'old preparer'}
        for name in transport.RUNTIME_FILES:
            self.file_ref('runtime/' + name, b'candidate ' + name.encode())
        app_sha = transport.identity(self.root / 'runtime/app.py')['sha256']
        oracle_sources = {source: {'documents': 1, 'tail_phrase_checks': 8, 'results': [{}] * 22}
                          for source in ['RMRB', 'PLA']}
        evidence = {'oracle-receipt.json': {'queries': 22, 'sources': oracle_sources, 'runtime_sha256': app_sha},
                    'oracle-process-receipt.json': {'exit_code': 0, 'runtime_sha256_unchanged': True,
                                                   'runtime_sha256': app_sha},
                    'full-runtime-acceptance-receipt.json': {'runtime_sha256': app_sha, 'browser': [
                        {'page_errors': []} for _ in range(4)], 'http': {'mixed_zip_complete_original_bytes_and_CRC': True}},
                    'full-package-acceptance-receipt.json': {'metadata': {'source_counts': {s: 1 for s in databases}}}}
        for name, value in evidence.items():
            self.file_ref('evidence/' + name, contract.canonical({**value, 'remote_writes': 0}))
        desired = [*metadata, *archives, *[rollout.reference(d['path'], d['sha256'], d['bytes'])
                                         for s, d in databases.items() if s in {'RMRB', 'PLA'}]]
        desired += [self.file_ref('runtime/' + n, (self.root / 'runtime' / n).read_bytes()) for n in transport.RUNTIME_FILES]
        desired += [self.file_ref('evidence/' + n, (self.root / 'evidence' / n).read_bytes()) for n in transport.EVIDENCE_FILES]
        self.parts = {}
        entries = []
        for ref in desired:
            raw = (self.root / ref['path']).read_bytes()
            url = URL.rsplit('/', 1)[0] + f'/part-{ref["sha256"]}.bin'
            self.parts[url] = raw
            entries.append({key: ref[key] for key in ['path','sha256','bytes']} | {
                'parts': [{'url': url, 'sha256': ref['sha256'], 'bytes': ref['bytes']}]})
        self.value = {'version': 1, 'kind': transport.KIND, 'sample_only': False, 'manifest': self.manifest,
            'builder_sha256': contract.digest(TINY_BUILDER), 'files': entries,
            'previous_runtime_sha256': {name: contract.digest(raw) for name, raw in self.old_runtime.items()}}
        self.space = Space({**self.old_runtime, 'build_fulltext_db.py': TINY_BUILDER,
            'ocr_release.py': Path(contract.__file__).read_bytes(), 'data/search_data.json.gz': old_raw}, self.store.events)
        self.approved = {'version': 1, 'consumer_revision': self.space.head(),
            'builder_sha256': contract.digest(TINY_BUILDER), 'base_metadata_sha256': self.old_sha}
        self.old_exports = self.ocr_fixture()

    def file_ref(self, path, raw):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return rollout.reference(path, contract.digest(raw), len(raw))

    def ocr_fixture(self):
        refs, registry = [], {}
        for n in [1, 7]:
            origin = f'book{n}.pdf'
            repo = 'VoiceOfML/Books'
            body = ('手机 原文 ' + str(n)).encode()
            text = ocr.resource(f'text/objects/{contract.digest(body)}.txt', body)
            provenance = ocr.resource(f'objects/fixture-{n}/page.json', b'original evidence')
            provenance['bucket'] = 'vomebook/pdf-pages-v2'
            self.store.objects[provenance['bucket'], provenance['path']] = b'original evidence'
            identity = {'recipe': 'ocr-plain-utf8-pages-v1', 'origin_repo': repo, 'origin_rel_path': origin,
                'origin_revision': 'b' * 40, 'origin_sha256': 'c' * 64, 'source_sha256': 'c' * 64,
                'doc_id': f'OCR:{n}', 'text_generation': 'd' * 64, 'txt_sha256': text['sha256']}
            book = contract.digest((repo + '\0' + origin).encode())
            generation = contract.digest(contract.canonical(identity).rstrip(b'\n'))
            value = {**identity, 'version': 1, 'kind': 'ccrd-ocr-text-export', 'complete': True,
                'generation': generation, 'book_id': book, 'source_sha256_role': 'reader-primary',
                'page_count': 1, 'accepted_pages': 0, 'quality': 'unreviewed', 'text': text,
                'readable_rel_path': repo + '/' + origin.replace('.pdf','.txt'),
                'offset_unit': 'unicode-codepoint', 'page_separator': '\n\n', 'pages': [
                    {'page': 1, 'start': 0, 'end': len(body.decode()), 'text_generation': 'd' * 64,
                     'quality': 'unreviewed', 'text_sha256': text['sha256']}],
                'retained_reader_resources': [provenance]}
            path = f'text/exports/{book}/{generation}/manifest.json'
            raw = contract.canonical(value)
            refs.append(ocr.resource(path, raw))
            self.store.objects[BUCKET, path] = raw
            self.store.objects[BUCKET, text['path']] = body
            registry[book] = {'doc_id': value['doc_id'], 'readable_rel_path': value['readable_rel_path']}
        identity = {'version': 1, 'kind': 'ccrd-ocr-search-release', 'base_metadata_sha256': self.old_sha,
            'builder_sha256': self.approved['builder_sha256'], 'tokenizer_version': contract.TOKENIZER,
            'exports': sorted(refs, key=lambda ref: ref['path'])}
        gen = contract.digest(contract.canonical(identity))
        release = {**identity, 'generation': gen, 'documents': 2,
                   'database': ocr.resource(f'indexes/ocr/{gen}/OCR.sqlite3', b'previous index')}
        ref = ocr.resource(f'releases/ocr-{gen}/manifest.json', contract.canonical(release))
        self.store.objects[BUCKET, ref['path']] = contract.canonical(release)
        self.store.objects[BUCKET, release['database']['path']] = b'previous index'
        self.old_ocr = {'version': 1, 'kind': 'ccrd-ocr-current', 'generation': gen, 'release': ref,
            'resources': [ref], 'parent': None, 'base_metadata_sha256': self.old_sha}
        self.store.objects[BUCKET, ocr.CURRENT] = contract.canonical(self.old_ocr)
        self.store.objects[BUCKET, ocr.IDS] = contract.canonical({'version': 1, 'kind': 'ccrd-ocr-id-registry',
            'ids': registry, 'resources': [ref]})
        return identity['exports']

    def publish(self, *, apply=True):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == Path('ccrd-ocr-consumer.json'):
                return json.dumps(self.approved)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), patch.object(ocr, 'assert_writer'):
            return rollout.rollout(self.store, self.space, self.value, self.root, apply=apply)

    def test_full_protocol_preserves_all_ocr_exports_and_commits_only_allowed_space_files(self):
        result = self.publish()
        self.assertTrue(result['OCR_exports_unchanged'])
        self.assertEqual(result['OCR_documents'], 2)
        current = json.loads(self.store.read_bytes(BUCKET, ocr.CURRENT))
        release = json.loads(self.store.read_bytes(BUCKET, current['release']['path']))
        self.assertEqual(release['exports'], self.old_exports)
        self.assertEqual(release['base_metadata_sha256'], self.manifest['base_metadata_sha256'])
        self.assertEqual(json.loads(self.store.read_bytes(BUCKET, ocr.IDS))['ids'],
                         {json.loads(self.store.read_bytes(BUCKET,r['path']))['book_id']: {
                             key: json.loads(self.store.read_bytes(BUCKET,r['path']))[key]
                             for key in ['doc_id','readable_rel_path']} for r in self.old_exports})
        events = self.store.events
        positions = {kind: next(i for i, e in enumerate(events) if e[0] == kind) for kind in ['file','space-commit']}
        self.assertEqual(events[0], ('put', rollout.JOURNAL))
        self.assertLess(positions['file'], positions['space-commit'])
        pointer = next(i for i,e in enumerate(events) if e==('put','current.json'))
        self.assertGreater(pointer, positions['space-commit'])
        files = next(e[1] for e in events if e[0]=='space-commit')
        self.assertEqual(set(files), transport.RUNTIME_FILES | transport.METADATA_FILES |
                         {'data/index-release.json','data/ocr-release.json'})
        journal = json.loads(self.store.read_bytes(BUCKET,rollout.JOURNAL))
        self.assertEqual(journal['status'],'activated')
        with patch.object(ocr,'assert_writer'), self.assertRaisesRegex(ValueError,'rollout'):
            auto.reconcile(self.store,self.root/'auto',self.root/'consumer',self.approved,plan_only=True)
        with self.assertRaisesRegex(gc.Blocker,'pending newspaper'):
            gc.plan(self.store)

    def test_upload_corruption_and_immutable_conflict_never_promote(self):
        path = self.manifest['databases']['RMRB']['path']
        self.store.corrupt_upload = path
        with self.assertRaisesRegex(ValueError,'readback mismatch'):
            self.publish()
        self.assertEqual(self.space.head(),'1'*40)
        self.assertEqual(json.loads(self.store.read_bytes(BUCKET,ocr.CURRENT)),self.old_ocr)
        before=list(self.store.events)
        with self.assertRaisesRegex(ValueError,'readback mismatch'):
            self.publish()
        self.assertFalse(any(e==('file',path) for e in self.store.events[len(before):]))

    def test_space_commit_interruption_keeps_old_pointer_and_retry_reuses_exact_rebase(self):
        previous=self.store.read_bytes(BUCKET,'current.json')
        self.space.fail_commit=True
        with self.assertRaisesRegex(ConnectionError,'interrupted Space'):
            self.publish()
        self.assertEqual(self.store.read_bytes(BUCKET,'current.json'),previous)
        self.assertEqual(self.space.head(),'1'*40)
        self.space.fail_commit=False
        result=self.publish()
        self.assertEqual(result['space_revision'],'2'*40)
        self.assertEqual(sum(e[0]=='space-commit' for e in self.store.events),1)
        before=len(self.store.events)
        self.publish()
        self.assertFalse(any(e[0]=='space-commit' for e in self.store.events[before:]))

    def test_fresh_runner_can_resume_from_durable_journal_without_local_OCR_index(self):
        self.space.fail_commit=True
        with self.assertRaises(ConnectionError):
            self.publish()
        self.space.fail_commit=False
        fresh=self.root/'fresh-runner'
        for entry in self.value['files']:
            target=fresh/entry['path'];target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((self.root/entry['path']).read_bytes())
        original=self.root
        self.root=fresh
        try:
            result=self.publish()
        finally:
            self.root=original
        self.assertEqual(result['space_revision'],'2'*40)
        self.assertEqual(result['OCR_documents'],2)

    def test_dry_run_and_stale_base_fail_without_remote_writes(self):
        self.assertFalse(self.publish(apply=False)['applied'])
        self.assertEqual(self.store.events,[])
        self.space.snapshots[self.space.head()]['app.py']=b'unapproved external edit'
        with self.assertRaisesRegex(ValueError,'runtime changed'):
            self.publish()
        self.assertEqual(self.store.events,[])

    def test_transport_checks_every_part_and_rejects_extra_or_foreign_inputs(self):
        transport.validate(self.value,URL)
        output=self.root/'download'
        def read(request,timeout):
            self.assertIsNone(request.get_header('Authorization'))
            return io.BytesIO(self.parts[request.full_url])
        transport.materialize(self.value,output,open_url=read)
        for entry in self.value['files']:
            self.assertEqual((output/entry['path']).read_bytes(),(self.root/entry['path']).read_bytes())
        broken=copy.deepcopy(self.value)
        broken['files'][0]['parts'][0]['url']='https://evil.example/part.bin'
        with self.assertRaisesRegex(ValueError,'transport part'):
            transport.validate(broken,URL)
        broken=copy.deepcopy(self.value);broken['files'].append(copy.deepcopy(broken['files'][0]))
        with self.assertRaisesRegex(ValueError,'allowlist'):
            transport.validate(broken,URL)
        broken=copy.deepcopy(self.value);broken['sample_only']=True
        with self.assertRaisesRegex(ValueError,'full approved'):
            transport.validate(broken,URL)

    def test_streamed_generation_preserves_source_roles_and_final_pin_releases_only_ocr_history(self):
        raw=contract.canonical(self.manifest)
        parsed,_=graph_json([raw[n:n+9] for n in range(0,len(raw),9)])
        order=lambda ref: (ref['bucket'],ref['path'])
        self.assertEqual(sorted(lifecycle.references(parsed,'current.json'),key=order),
                         sorted(lifecycle.references(self.manifest,'current.json'),key=order))
        result=self.publish()
        acceptance={'kind':'ccrd-newspaper-live-acceptance','generation':result['generation'],
            'base_metadata_sha256':result['base_metadata_sha256'],'mixed_zip_verified':True,
            'first_last_API_pages_checked':True,'OCR':{'generation':result['OCR_generation'],
            'complete_id_set_verified':True}}
        consumer=pin.accepted_pin(result,acceptance)
        pin.finish_journal(self.store,result,consumer)
        journal=json.loads(self.store.read_bytes(BUCKET,rollout.JOURNAL))
        self.assertEqual(journal['status'],'committed')
        self.assertFalse(any(r['path'].startswith(('releases/ocr-','indexes/ocr/')) for r in journal['resources']))
        lifecycle.references(journal,rollout.JOURNAL)
        with self.assertRaisesRegex(ValueError,'production acceptance'):
            pin.accepted_pin(result,{**acceptance,'mixed_zip_verified':False})

    def test_packager_requires_explicit_successful_attempt_and_materializes_complete_small_parts(self):
        papers = self.root / 'evidence'
        failed = papers / 'oracle-process-receipt.json'
        success = papers / 'oracle-process-receipt-attempt2.json'
        success.write_bytes(failed.read_bytes())
        failed.write_bytes(contract.canonical({'remote_writes': 0, 'exit_code': -15}))
        refs = lifecycle.references(self.manifest,'current.json')
        old_paths={self.manifest['databases'][s]['path'] for s in ['CCRD','CW']}
        plan={'kind':'ccrd-four-source-local-publication-plan','sample_only':False,
            'complete_newspaper_audits':True,'documents':{s:1 for s in self.manifest['databases']},
            'manifest':self.manifest,'upload_files':[dict(ref,local_path=str(self.root/ref['path']))
                for ref in refs if ref['path'] not in old_paths]}
        with self.assertRaisesRegex(ValueError,'must pass'):
            packager.package(plan,papers,self.root/'runtime',self.value['previous_runtime_sha256'],
                             URL,self.root/'rejected')
        output=self.root/'transport-parts'
        with patch.object(transport,'MAX_PART',16384):
            result=packager.package(plan,papers,self.root/'runtime',self.value['previous_runtime_sha256'],
                URL,output,oracle_process_receipt=success)
            value=json.loads(Path(result['descriptor']).read_text())
            for entry in value['files']:
                self.assertEqual(sum(p['bytes'] for p in entry['parts']),entry['bytes'])
                self.assertTrue(all(p['bytes']<=16384 for p in entry['parts']))
            def read(request,timeout):
                return io.BytesIO((output/request.full_url.rsplit('/',1)[1]).read_bytes())
            with patch('builtins.print'):
                transport.materialize(value,self.root/'reassembled',open_url=read)
        admitted=json.loads((self.root/'reassembled/evidence/oracle-process-receipt.json').read_text())
        self.assertEqual(admitted['exit_code'],0)
        self.assertEqual(json.loads(failed.read_text())['exit_code'],-15)

    def test_pending_rollout_prevents_manual_old_base_OCR_publication(self):
        self.space.fail_commit=True
        with self.assertRaises(ConnectionError):
            self.publish()
        with self.assertRaisesRegex(ValueError,'rollout'):
            ocr.publish(self.store,self.root,'releases/old/manifest.json','a'*64,
                        self.old_sha,self.old_ocr['generation'],apply=False)

    def test_workflow_keeps_one_writer_and_within_dispatch_input_limit(self):
        import yaml
        path=Path(__file__).parents[1]/'.github/workflows/publish-ccrd-ocr.yml'
        value=yaml.safe_load(path.read_text())
        triggers=value.get('on') or value[True]
        self.assertLessEqual(len(triggers['workflow_dispatch']['inputs']),10)
        self.assertEqual(value['concurrency']['group'],'publish-ccrd-index')
        self.assertEqual(value['concurrency']['queue'],'max')
        job=value['jobs']['newspapers']
        self.assertIn("startsWith(inputs.release_path, 'https://')",job['if'])
        names=[step.get('name','') for step in job['steps']]
        self.assertLess(next(i for i,n in enumerate(names) if n.startswith('Verify the exact production')),
                        next(i for i,n in enumerate(names) if n.startswith('Pin the accepted')))

    def test_space_read_retry_covers_429_and_transport_but_never_blindly_repeats_commit(self):
        import httpx
        from huggingface_hub.errors import HfHubHTTPError
        request=httpx.Request('GET','https://huggingface.co/api/spaces/vomebook/Search')
        limited=HfHubHTTPError('fixture limited',response=httpx.Response(429,request=request,
                                                                     headers={'Retry-After':'1'}))
        api=Mock()
        api.repo_info.side_effect=[limited,SimpleNamespace(sha='a'*40),
                                  limited,SimpleNamespace(runtime=SimpleNamespace(raw={'stage':'RUNNING','sha':'a'*40}))]
        local=self.root/'downloaded-runtime.py';local.write_bytes(b'exact runtime bytes')
        with patch('huggingface_hub.HfApi',return_value=api), \
                patch('scripts.reader_bucket_store.time.sleep') as sleep:
            space=rollout.SpaceCommitter()
            self.assertEqual(space.head(),'a'*40)
            self.assertEqual(space.runtime_revision(),'a'*40)
            with patch('huggingface_hub.hf_hub_download',side_effect=[httpx.ReadTimeout('fixture'),str(local)]) as download:
                self.assertEqual(space.read('app.py','a'*40),b'exact runtime bytes')
                self.assertEqual(download.call_count,2)
                self.assertTrue(all(call.kwargs['revision']=='a'*40 for call in download.call_args_list))
            self.assertEqual(sleep.call_count,3)
            api.create_commit.side_effect=limited
            with self.assertRaises(HfHubHTTPError):
                space.commit('a'*40,{'app.py':b'exact runtime bytes'})
            api.create_commit.assert_called_once()

    def test_live_acceptance_uses_running_revision_not_nonexistent_metadata_HTTP_route(self):
        # Add real body/path metadata for this tiny accepted snapshot. The live
        # client stays entirely mocked, including complete OCR-ID acceptance.
        self.publish()
        result=self.publish()
        path=self.root/self.prefix/'data/search_data.json.gz'
        metadata=json.loads(gzip.decompress(path.read_bytes()))
        for record in metadata['records']:
            if record['source'] in {'RMRB','PLA'}:
                record.update(storage_rel_path='1976/197612/19761231/标题.txt',display_name='标题',
                    source_name=record['source'],display_rel_path='1976/197612/19761231/标题.txt',
                    size=len('正文'.encode()),text_sha256=contract.digest('正文'.encode()))
        raw=gzip.compress(contract.canonical(metadata),mtime=0)
        path.write_bytes(raw)
        self.manifest['base_metadata_sha256']=contract.digest(raw)
        for ref in self.manifest['metadata']:
            if ref['path'].endswith('/data/search_data.json.gz'):
                ref.update(sha256=contract.digest(raw),bytes=len(raw))
        (self.root/self.prefix/'manifest.json').write_bytes(contract.canonical(self.manifest))
        self.space.snapshots[self.space.head()]['data/search_data.json.gz']=raw
        # Live SQL probes require the newspaper source catalog only for the max.
        for source in ['RMRB','PLA']:
            with sqlite3.connect(self.root/self.manifest['databases'][source]['path']) as db:
                db.execute('CREATE TABLE source_files(number INTEGER,bytes INTEGER)')
                db.execute('INSERT INTO source_files VALUES(1,6)')
        self.space.runtime_revision=lambda: self.space.head()
        status={'ready':True,'ocr_release':result['OCR_generation'],'bucket':{
            'source':'space-activation','generation':self.generation,
            'base_metadata_sha256':self.manifest['base_metadata_sha256']}}
        def request(path,payload=None):
            if path=='/api/index-status':
                nonlocal transient_status_requests
                transient_status_requests += 1
                if transient_status_requests == 1:
                    raise HTTPError('https://fixture.invalid/api/index-status', 503,
                                    'fixture previous image unavailable during build', {}, None)
                return status
            if path=='/api/sources': return [{'slug':s,'count':1} for s in self.manifest['databases']]
            if path.startswith('/api/search/'):
                source=path.rsplit('/',1)[1]
                return {'indexing':False,'total':1,'results':[{'doc_id':source+':1'}]}
            if path.startswith('/api/preview/'): return {'text':'正文'}
            if path=='/api/zip-prepare':
                return {'file_count':len(payload['doc_ids']),'download_url':'/fixture.zip'}
            raise AssertionError(path)
        def request_bytes(path,payload=None):
            if path=='/fixture.zip':
                stream=io.BytesIO()
                with __import__('zipfile').ZipFile(stream,'w') as zipped:
                    for source in ['RMRB','PLA']:
                        zipped.writestr(f'{source}/标题 [{source}_1].txt','正文'.encode())
                return stream.getvalue()
            if path.startswith(('/api/download/','/txt/')): return '正文'.encode()
            raise AssertionError('unexpected metadata HTTP route: '+path)
        transient_status_requests=0
        with patch.object(live.ocr_live,'json_request',side_effect=request), \
                patch.object(live.ocr_live,'request',side_effect=request_bytes), \
                patch.object(live.ocr_live,'all_results',return_value=[]), \
                patch.object(live.time,'sleep'), \
                patch.object(live.ocr_live,'verify',return_value={'generation':result['OCR_generation'],
                    'complete_id_set_verified':True}):
            accepted=live.verify(self.root,result,space=self.space,wait_seconds=30)
        self.assertTrue(accepted['mixed_zip_verified'])
        self.assertGreaterEqual(transient_status_requests,3)
        self.assertEqual(accepted['full_base_metadata_ids'],4)
        self.assertEqual(set(accepted['selected_original_TXT_ids']),{'RMRB:1','PLA:1'})
        for code,expected_error in [(503,TimeoutError),(401,HTTPError)]:
            failure=HTTPError('https://fixture.invalid/api/index-status',code,'fixture response',{},None)
            with self.subTest(status=code), patch.object(live.ocr_live,'json_request',side_effect=failure), \
                    self.assertRaises(expected_error):
                live.verify(self.root,result,space=self.space,wait_seconds=0)

    def test_consumer_state_commit_preserves_unrelated_upstream_and_rejects_competing_pin(self):
        remote=self.root/'git-remote'
        author=self.root/'git-author'
        worker=self.root/'git-worker'
        def git(cwd,*args):
            return subprocess.check_output(['git',*args],cwd=cwd,stderr=subprocess.STDOUT,text=True)
        git(self.root,'init','--bare','--initial-branch=main',str(remote))
        git(self.root,'clone',str(remote),str(author))
        git(author,'config','user.name','fixture')
        git(author,'config','user.email','fixture@example.invalid')
        (author/'ccrd-ocr-consumer.json').write_text(json.dumps(self.approved))
        git(author,'add','ccrd-ocr-consumer.json')
        git(author,'commit','-m','fixture initial approved pin')
        git(author,'push','origin','main')
        git(self.root,'clone',str(remote),str(worker))
        (author/'unrelated.txt').write_text('concurrent user work stays intact')
        git(author,'add','unrelated.txt')
        git(author,'commit','-m','unrelated concurrent work')
        git(author,'push','origin','main')
        accepted={**self.approved,'consumer_revision':'2'*40,
                  'base_metadata_sha256':self.manifest['base_metadata_sha256']}
        previous=Path.cwd()
        try:
            os.chdir(worker)
            pin.publish_pin(accepted)
            self.assertEqual(json.loads(git(worker,'show','origin/main:ccrd-ocr-consumer.json')),accepted)
            self.assertEqual(git(worker,'show','origin/main:unrelated.txt'),'concurrent user work stays intact')
            pin.publish_pin(accepted)
            # A later approved pin cannot be silently overwritten by this run.
            git(author,'pull','--ff-only','origin','main')
            other={**accepted,'consumer_revision':'3'*40}
            (author/'ccrd-ocr-consumer.json').write_text(json.dumps(other))
            git(author,'add','ccrd-ocr-consumer.json')
            git(author,'commit','-m','different approved consumer')
            git(author,'push','origin','main')
            with self.assertRaisesRegex(ValueError,'another writer'):
                pin.publish_pin(accepted)
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
