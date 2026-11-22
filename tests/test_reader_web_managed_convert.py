import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import reader_web_managed_convert as managed


REV = 'a' * 40
DOC = '<h1 id="a">标题</h1><p><a href="#a">Back</a></p><table><tr><td>cell</td></tr></table><pre>\n\n  x\t</pre>'


class Response(io.BytesIO):
    def __init__(self, raw, status=200, headers=None):
        super().__init__(raw)
        self.status = status
        self.headers = {'Content-Length': str(len(raw))} if headers is None else headers


def item(raw, extension='html', stem='page'):
    return {'repo': 'org/dataset', 'path': f'书/{stem}.{extension}', 'extension': extension,
            'source_revision': REV, 'source_sha256': managed.digest(raw)}


def request(*items, **limits):
    return {'version': 1, 'max_rows': 10, 'max_source_bytes': 50000, 'max_output_bytes': 50000,
            'items': list(items), **limits}


def png():
    buffer = io.BytesIO()
    managed.producer.Image.new('RGB', (1, 1)).save(buffer, format='PNG')
    return buffer.getvalue()


class ManagedWebTests(unittest.TestCase):
    def test_all_extensions_and_deterministic_verified_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            html = DOC.encode()
            mime = b'Content-Type: text/html; charset=utf-8\r\n\r\n' + html
            rows = [item(html, 'htm'), item(html), item(mime, 'mht'), item(mime, 'mhtml')]
            fetch = Mock(side_effect=lambda url: Response(mime if url.endswith(('.mht', '.mhtml')) else html))
            report = managed.run(request(*rows), output, fetch=fetch)
            self.assertFalse(report['dual_reader_verified'])
            self.assertFalse(report['publication_ready'])
            self.assertEqual(report['remote_writes'], 0)
            paths = []
            for row in report['items']:
                self.assertEqual(row['status'], 'converted', row)
                files = row['files']
                for metadata in files.values():
                    path = output / metadata['path']
                    raw = path.read_bytes()
                    self.assertEqual(managed.digest(raw), metadata['sha256'])
                    self.assertEqual(len(raw), metadata['bytes'])
                    paths.append((path, path.stat().st_ino))
                manifest = json.loads((output / files['document-manifest.json']['path']).read_bytes())
                self.assertEqual(manifest['source_sha256'], row['source_sha256'])
                self.assertEqual(manifest['sha256'], files['document.html']['sha256'])
                body = managed.parse_html((output / files['document.html']['path']).read_text()).find('body')
                self.assertEqual(body.find('pre').text, '\n  x\t')
                self.assertEqual(body.find('p/a').get('href'), '#a')
                self.assertEqual(body.find('div/table/tbody/tr/td').text, 'cell')
            first = (output / 'report.json').read_bytes()
            self.assertEqual(managed.run(request(*rows), output, fetch=fetch), report)
            self.assertEqual((output / 'report.json').read_bytes(), first)
            self.assertTrue(all(path.stat().st_ino == inode for path, inode in paths))
            self.assertTrue(all('/resolve/' + REV + '/%E4%B9%A6/' in c.args[0] for c in fetch.call_args_list))

    def test_only_same_tree_resources_and_no_scripts(self):
        raw = (DOC + '<img src="picture.png" alt="art"><link rel="stylesheet" href="layout.css">'
               '<script src="external.js"></script><base href="https://evil.example/">').encode()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            files = {'page.html': raw, 'picture.png': png(), 'layout.css': b'@import "https://evil.example/a"; p {color: red}'}
            fetch = Mock(side_effect=lambda url: Response(files[url.rsplit('/', 1)[-1]]))
            row = managed.run(request(item(raw)), output, fetch=fetch)['items'][0]
            self.assertEqual(row['status'], 'converted', row)
            converted = (output / row['files']['document.html']['path']).read_text()
            self.assertIn('data:image/png;base64,', converted)
            self.assertNotIn('evil.example', converted)
            self.assertNotIn('<script', converted)
            self.assertEqual([c.args[0].rsplit('/', 1)[-1] for c in fetch.call_args_list], list(files))
            for link in ('https://evil.example/x.png', '../x.png', '%2e%2e/x.png', '%252e%252e/x.png'):
                with self.subTest(link=link):
                    source = f'<p>text</p><img src="{link}">'.encode()
                    fetch = Mock(return_value=Response(source))
                    row = managed.run(request(item(source)), output, fetch=fetch)['items'][0]
                    self.assertEqual(row['status'], 'converted')
                    self.assertEqual(row['omissions'], {'missing_images':1})
                    fetch.assert_called_once()

    def test_mhtml_root_and_cid(self):
        raw = (b'Content-Type: multipart/related; boundary=x; start="<root>"\r\n\r\n'
               b'--x\r\nContent-Type: text/html\r\n\r\n<p>not root</p>\r\n'
               b'--x\r\nContent-Type: text/html\r\nContent-ID: <root>\r\n\r\n'
               b'<h1>root</h1><img src=cid:img>\r\n'
               b'--x\r\nContent-Type: image/png\r\nContent-ID: <img>\r\n'
               b'Content-Transfer-Encoding: base64\r\n\r\n' + base64.b64encode(png()) + b'\r\n--x--\r\n')
        with tempfile.TemporaryDirectory() as tmp:
            fetch = Mock(return_value=Response(raw))
            out = Path(tmp)
            row = managed.run(request(item(raw, 'mhtml')), out, fetch=fetch)['items'][0]
            self.assertEqual(row['status'], 'converted', row)
            text = (out / row['files']['document.html']['path']).read_text()
            self.assertIn('<h1>root</h1>', text)
            self.assertIn('data:image/png;base64,', text)
            self.assertNotIn('not root', text)
            fetch.assert_called_once()

    def test_comment_and_loader_ids_are_not_visible_text_or_navigation(self):
        raw = (DOC + '<p>before<!-- archive metadata -->after</p> '
                      '<table><tr><td><form><table><tr><td>nested</td></tr></table></form></td></tr></table>'
                      '<input id="formState">').encode()
        output = managed.convert(raw, 'html', 'https://huggingface.co/datasets/org/books/resolve/' + REV + '/x.html', None, 50000)
        self.assertIn(b'beforeafter', output)
        self.assertIn(b'nested', output)
        self.assertNotIn(b'archive metadata', output)
        with self.assertRaisesRegex(managed.Hold, 'parity_anchors'):
            managed.convert(raw + b'<a href="#formState">jump</a>', 'html', 'https://huggingface.co/', None, 50000)

    def test_legacy_font_target_and_empty_srcset(self):
        raw = (DOC + '<font id="old">old title</font><a href="#old">go</a><img srcset=" , " src="data:image/png;base64,'
               + base64.b64encode(png()).decode() + '">').encode()
        output=managed.convert(raw,'htm','https://huggingface.co/',None,50000)
        self.assertIn(b'<span id="old">old title</span>', output)
        self.assertIn(b'href="#old"', output)
        self.assertIn(b'data:image/png',output)
        with self.assertRaisesRegex(managed.Hold,'unsupported_embedded_content'):
            managed.convert(raw.replace(b' , ',b'alt.png 2x'),'htm','https://huggingface.co/',None,50000)

    def test_mhtml_preserves_content_excludes_known_counter_and_empty_frame(self):
        html = (DOC + '<img src="http://zyz.wyzxsx.com/tj/piwik.php?idsite=5" alt="">'
                      '<iframe id="UserLogin" src="http://www.wyzxsx.com/Article/ClassTree.asp"></iframe>'
                      '<img src="cid:cover" alt="illustration">').encode()
        p = base64.b64encode(png())
        def mime(frame):
            return (b'Content-Type: multipart/related; boundary=x\r\n\r\n--x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n'
                    + html + b'\r\n--x\r\nContent-Type: text/html\r\nContent-Location: http://www.wyzxsx.com/Article/ClassTree.asp\r\n\r\n'
                    + frame + b'\r\n--x\r\nContent-Type: image/png\r\nContent-ID: <cover>\r\nContent-Transfer-Encoding: base64\r\n\r\n'
                    + p + b'\r\n--x--\r\n')
        output = managed.convert(mime(b'<script>navigation();</script><br>'), 'mht', '', None, 50000)
        self.assertIn(b'id="UserLogin"', output)
        self.assertIn(b'alt="illustration"', output)
        self.assertNotIn(b'piwik.php', output)
        self.assertNotIn(b'<iframe', output)
        with self.assertRaisesRegex(managed.Hold, 'unsupported_embedded_content'):
            managed.convert(mime(b'<p>visible archived article</p>'), 'mht', '', None, 50000)
        omissions={}
        cleaned=managed.convert(mime(b'<br>').replace(b'piwik.php?idsite=5', b'photo.png'), 'mht', '', None, 50000, omissions=omissions)
        self.assertEqual(omissions,{'missing_images':1})
        self.assertNotIn(b'photo.png',cleaned)
        self.assertIn(b'alt="illustration"',cleaned)

    def test_missing_picture_preserves_alt_caption_and_fragment(self):
        raw = (DOC + '<figure><img id="missing" src="https://archive.invalid/gone.png" alt="&lt;图&gt;">'
                     '<figcaption>原图说明</figcaption></figure><a href="#missing">图</a>').encode()
        omitted={}
        doc=managed.convert(raw,'html','https://huggingface.co/',None,50000,omissions=omitted)
        body=managed.parse_html(doc.decode()).find('body')
        self.assertEqual(omitted,{'missing_images':1})
        self.assertEqual(body.find('figure/span').attrib,{'id':'missing'})
        self.assertEqual(body.find('figure/span').text,'<图>')
        self.assertEqual(body.find('figure/figcaption').text,'原图说明')
        self.assertEqual(body.find('a').get('href'),'#missing')
        self.assertNotIn(b'<img',doc)
        with self.assertRaisesRegex(managed.Hold,'missing_content'):
            managed.convert(b'<img src="https://archive.invalid/gone.png">','html','https://huggingface.co/',None,50000)

    def test_only_confirmed_sibling_absence_is_omitted(self):
        raw=(DOC + '<img src="photo.png" alt="图">').encode()
        def fetch(status,error):
            def fn(url):
                if url.endswith('/photo.png'):
                    raise managed.urllib.error.HTTPError(url,status,'error',{'X-Error-Code':error},None)
                return Response(raw)
            return fn
        for status,error,wanted in ((404,'EntryNotFound','converted'),(404,'RevisionNotFound','hold'),(503,'','hold')):
            with self.subTest(status=status,error=error), tempfile.TemporaryDirectory() as tmp:
                row=managed.run(request(item(raw)),Path(tmp),fetch=fetch(status,error))['items'][0]
                self.assertEqual(row['status'],wanted)
                if wanted=='converted':self.assertEqual(row['omissions'],{'missing_images':1})
                else:self.assertEqual(row['reason'],'download_failed')

    def test_present_but_unsupported_embedded_image_still_holds(self):
        for src in ('data:image/svg+xml,content','blob:existing','mhtml:existing!photo.png'):
            with self.subTest(src=src),self.assertRaisesRegex(managed.Hold,'unsupported_embedded_content'):
                managed.convert((DOC+f'<img src="{src}">').encode(),'html','https://huggingface.co/',None,50000)
        raw = (b'Content-Type: multipart/related; boundary=x\r\n\r\n'
               b'--x\r\nContent-Type: text/html\r\n\r\n<p>Body</p><img src="cid:part">\r\n'
               b'--x\r\nContent-Type: text/plain\r\nContent-ID: <part>\r\n\r\narchive bytes\r\n--x--\r\n')
        with self.assertRaisesRegex(managed.Hold, 'unsupported_embedded_content'):
            managed.convert(raw,'mht','',None,50000)

    def test_mhtml_duplicate_attachment_must_match_bytes(self):
        p = base64.b64encode(png())
        head = b'Content-Type: multipart/related; boundary=x\r\n\r\n--x\r\nContent-Type: text/html\r\n\r\n<img src=cid:cover>\r\n'
        part = b'--x\r\nContent-Type: image/png\r\nContent-ID: <cover>\r\nContent-Transfer-Encoding: base64\r\n\r\n'
        raw = head + part + p + b'\r\n' + part + p + b'\r\n--x--\r\n'
        self.assertIn(b'data:image/png', managed.convert(raw, 'mhtml', '', None, 50000))
        with self.assertRaisesRegex(managed.Hold, 'mhtml_ambiguous_resource'):
            managed.convert(raw.rsplit(p,1)[0] + b'YQ==\r\n--x--\r\n', 'mhtml', '', None, 50000)

    def test_source_validation_budgets_and_hold_isolation(self):
        raw = DOC.encode()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            wrong = item(raw + b'x', stem='wrong')
            good = item(raw, stem='good')
            fetch = lambda _: Response(raw)
            rows = managed.run(request(wrong, good), out, fetch=fetch)['items']
            self.assertEqual(rows[0]['reason'], 'source_sha256_mismatch')
            self.assertEqual(rows[1]['status'], 'converted')
            rows = managed.run(request(wrong, good, max_source_bytes=len(raw)), out, fetch=fetch)['items']
            self.assertEqual(rows[1]['reason'], 'source_budget')
            rows = managed.run(request(good, max_output_bytes=1), out, fetch=fetch)['items']
            self.assertEqual(rows[0]['reason'], 'output_budget')
            for response in (Response(raw, status=206), Response(raw, headers={'Content-Encoding': 'gzip'}),
                             Response(raw, headers={'Content-Length': str(len(raw) + 1)})):
                rows = managed.run(request(good), out, fetch=lambda _: response)['items']
                self.assertEqual(rows[0]['reason'], 'download_response')

    def test_parity_loss_holds_and_corruption_is_not_overwritten(self):
        raw = DOC.encode()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            req = request(item(raw))
            fetch = lambda _: Response(raw)
            for fragment, reason in (('<h1>标题</h1>', 'parity_text'), (DOC.replace('id="a"', ''), 'parity_anchors'),
                                     (DOC.replace('<table><tr><td>cell</td></tr></table>', 'cell'), 'parity_tables'),
                                     (DOC.replace('  x\t', ' x'), 'parity_pre')):
                with self.subTest(reason=reason), patch.object(managed.producer, 'sanitize_web_html', return_value=fragment):
                    row = managed.run(req, out, fetch=fetch)['items'][0]
                self.assertEqual(row['reason'], reason)
            row = managed.run(req, out, fetch=fetch)['items'][0]
            path = out / row['files']['document-manifest.json']['path']
            tampered = b'X' * path.stat().st_size
            path.write_bytes(tampered)
            row = managed.run(req, out, fetch=fetch)['items'][0]
            self.assertEqual(row['reason'], 'local_entry_conflict')
            self.assertEqual(path.read_bytes(), tampered)

    def test_strict_schema_and_redirect_policy(self):
        row = item(b'x')
        for req in (request(row, max_rows=True), request(row, max_output_bytes=managed.MAX_OUTPUT_BYTES + 1),
                    request(row, row), request(dict(row, source_revision='main')),
                    request(dict(row, path='a/../b.html')), request(dict(row, repo='evil.com@org/repo'))):
            with self.subTest(req=req), self.assertRaises(ValueError):
                managed.validate_request(req)
        handler = managed.Redirect()
        initial = managed.urllib.request.Request('https://huggingface.co/datasets/org/data/resolve/' + REV + '/x.html')
        for bad in ('https://evil.example/x', 'http://cdn-lfs.hf.co/x', 'https://huggingface.co/api/whoami',
                    'https://user@cdn-lfs.hf.co/x', 'https://cdn-lfs.hf.co:123/x'):
            with self.subTest(url=bad), self.assertRaises(managed.Hold):
                handler.redirect_request(initial, None, 302, '', {}, bad)
        url = 'https://cas-bridge.xethub.hf.co/signed?token=ephemeral'
        self.assertEqual(handler.redirect_request(initial, None, 302, '', {}, url).full_url, url)
        cache = 'https://huggingface.co/api/resolve-cache/datasets/org/data/' + REV + '/x.html?etag=abc'
        self.assertEqual(handler.redirect_request(initial, None, 302, '', {}, cache).full_url, cache)
        with self.assertRaises(managed.Hold):
            handler.redirect_request(initial, None, 302, '', {}, cache.replace(REV, 'b' * 40))

    def test_cli_binds_exact_request_bytes(self):
        raw = DOC.encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'request.json', root / 'out'
            payload = json.dumps(request(item(raw))).encode() + b'\n\n'
            source.write_bytes(payload)
            original = managed.run
            with patch.object(managed, 'run', side_effect=lambda *a, **kw: original(
                    *a, fetch=lambda _: Response(raw), **kw)):
                self.assertEqual(managed.main(['--request', str(source), '--output', str(output)]), 0)
            report = json.loads((output / 'report.json').read_bytes())
            self.assertEqual(report['request_sha256'], managed.digest(payload))
            self.assertEqual(report['items'][0]['status'], 'converted')


if __name__ == '__main__':
    unittest.main()
