import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import build_reader_web_profile_candidate_index as intake

from scripts import reader_web_profile_candidates as cache
from scripts import reader_web_profile_publish as web


class ReviewedCandidates(unittest.TestCase):
    def setUp(self):
        self.item = {'repo':'owner/books', 'path':'book.html', 'extension':'html', 'revision':'a'*40}
        self.old = {'key':'owner/books\0book.html', 'extension':'html', 'source_sha256':'b'*64,
                    'sha256':'c'*64, 'bytes':100, 'checkpointed_at':'yesterday'}
        self.source = cache.source_identity(self.item,self.old)
        self.document = b'<!doctype html><p>Exact reusable content</p>'
        self.sha = cache.digest(self.document)
        self.entry = {'sha256':self.sha,'bytes':len(self.document),'omissions':{'missing_images':1}}
        self.body = {'version':1,'profile':cache.WEB_PROFILE,'converter':'e'*64,
                     'entries':{cache.input_key(self.source):self.entry}}

    def catalog(self, *, identity='e'*64, body=None):
        raw = cache.convert.encode(self.body if body is None else body)
        getter = Mock(return_value=raw)
        entries = cache.load(getter,cache.digest(raw),identity=identity)
        getter.assert_called_once_with(cache.index_path(cache.digest(raw)),cache.MAX_INDEX)
        return entries

    def test_exact_identity_and_body_digest_are_required(self):
        entries = self.catalog()
        get = Mock(return_value=self.document)
        self.assertEqual(cache.reuse(get,entries,self.source),(self.document,self.entry['omissions']))
        get.assert_called_once_with(cache.object_path(self.sha),len(self.document))
        get.reset_mock()
        for delta in ({'source_revision':'d'*40}, {'source_sha256':'7'*64}, {'repo':'some/other'}, {'path':'other.html'}):
            self.assertIsNone(cache.reuse(get,entries,self.source|delta))
        get.assert_not_called()
        get.return_value = self.document[:-1]+b'!'
        with self.assertRaisesRegex(ValueError,'candidate body differs'):
            cache.reuse(get,entries,self.source)

    def test_unconfigured_or_changed_converter_is_a_miss_but_bad_pin_is_not(self):
        get = Mock()
        self.assertEqual(cache.load(get,None),{})
        get.assert_not_called()
        self.assertEqual(self.catalog(identity='f'*64),{})
        raw=cache.convert.encode(self.body)
        with self.assertRaisesRegex(ValueError,'configured pin'):
            cache.load(lambda *_:raw,'b'*64,identity='e'*64)
        malformed=self.body|{'entries':{cache.input_key(self.source):self.entry|{'bytes':web.schema.MAX_DOC+1}}}
        with self.assertRaisesRegex(ValueError,'candidate entry'):
            self.catalog(body=malformed)

    def test_reviewed_document_does_not_download_or_convert_and_keeps_canonical_revision(self):
        old=self.old|{'source_revision':'f'*40}
        with patch.object(web.convert,'Downloads',side_effect=AssertionError('do not fetch')):
            new,raw,m,notes=web.candidate(self.item,old,reviewed=(self.document,self.entry['omissions']))
        self.assertEqual((raw,new['sha256'],new['source_revision'],notes),
                         (self.document,self.sha,'f'*40,self.entry['omissions']))
        self.assertIn(self.sha.encode(),m)
        self.assertEqual(new['profile'],cache.WEB_PROFILE)

    def test_intake_requires_the_current_converter_to_reproduce_saved_bytes(self):
        original=b'<!doctype html><html><body><p>Preserved paragraph</p></body></html>'
        source=self.source|{'source_sha256':cache.digest(original)}
        class Response(io.BytesIO):
            status=200
            headers={'Content-Length':str(len(original))}
        fetch=lambda url:Response(original)
        with tempfile.TemporaryDirectory() as stage:
            root=Path(stage)
            with patch.object(intake.convert,'convert',return_value=self.document) as replay:
                (root/'entry.html').write_bytes(self.document)
                row={'source':source,'file':'entry.html','sha256':self.sha}
                status,admit=intake.qualify(row,root,fetch=fetch)
                self.assertEqual((status,admit['sha256']),('matched',self.sha))
                self.assertIn(source['source_revision'],replay.call_args.args[2])
                replay.return_value=b'<p>Changed by current producer</p>'
                self.assertEqual(intake.qualify(row,root,fetch=fetch),('different-current-output',None))
            self.assertEqual(intake.qualify(row|{'source':source|{'source_sha256':'d'*64}},root,fetch=fetch),
                             ('source-hash-mismatch',None))

    def test_unreviewed_input_runs_the_existing_conversion(self):
        old=self.old|{'source_sha256':cache.digest(b'original')}
        with patch.object(web.convert,'Downloads') as dl, patch.object(web.convert,'convert',return_value=self.document) as convert:
            dl.return_value.get.return_value=b'original'
            self.assertEqual(web.candidate(self.item,old)[1],self.document)
            convert.assert_called_once()