import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import convert_reader_assets as convert, pilot_web_stream as pilot


def parse(document):
    return convert.html5lib_shim.HTMLParser(namespaceHTMLElements=False).parse(document)


class WebTypographyTests(unittest.TestCase):
    def test_preserves_document_structure_and_layout_hints(self):
        document = '''<!doctype html><html lang="zh-CN"><head><title>旧网页</title></head>
        <body dir="rtl" class="original"><main><article><h1 id="top">标题</h1>
        <p align="right" style="text-indent:2em; line-height:1.8">正文&#32 <a href="#top">回到标题</a></p>
        <center class="byline">作者</center><ol start="3"><li value="7">项目</ol>
        <table width="715" align="center" cellspacing="0"><caption>表格</caption>
        <tr><th scope="row">标题<td colspan="2" valign="top" width="60%">数据</table>
        <figure><img src="data:image/png;base64,AA==" width="500" height="400" alt="图">
        <figcaption>插图</figcaption></figure><pre>\n\n  &lt;tag&gt; &amp;\n\t<code>例</code></pre>
        </article></main></body></html>'''
        after = parse(convert.sanitize_web_html(document))
        self.assertEqual(after.get('lang'), 'zh-CN')
        self.assertEqual(after.find('head/title').text, '旧网页')
        self.assertEqual(after.find('head/meta').get('charset'), 'utf-8')
        self.assertEqual(after.find('head/meta[@name="viewport"]').get('content'),
                         'width=device-width, initial-scale=1')
        body = after.find('body')
        self.assertEqual(body.attrib, {'dir': 'rtl', 'class': 'original'})
        article = body.find('main/article')
        self.assertEqual(article.find('h1').get('id'), 'top')
        self.assertEqual(article.find('p/a').get('href'), '#top')
        self.assertEqual(article.find('p').text, '正文  ')
        self.assertEqual(article.find('p').attrib, {'align': 'right', 'style': 'text-indent:2em;line-height:1.8;'})
        self.assertEqual(article.find('center').text, '作者')
        self.assertEqual(article.find('ol').get('start'), '3')
        self.assertEqual(article.find('ol/li').get('value'), '7')
        wrapper = article.find('div[@data-reader-table-scroll=""]')
        self.assertIsNotNone(wrapper)
        table = wrapper.find('table')
        self.assertEqual(table.attrib, {'width': '715', 'align': 'center', 'cellspacing': '0'})
        self.assertEqual(table.find('caption').text, '表格')
        self.assertEqual(table.find('tbody/tr/th').get('scope'), 'row')
        self.assertEqual(table.find('tbody/tr/td').attrib, {'colspan': '2', 'valign': 'top', 'width': '60%'})
        self.assertEqual(article.find('figure/img').get('width'), '500')
        self.assertEqual(article.find('figure/figcaption').text, '插图')
        self.assertEqual(''.join(article.find('pre').itertext()), '\n  <tag> &\n\t例')
        self.assertEqual(after.find('head/style').text, convert.WEB_READER_CSS)
        self.assertNotIn('width="715"', convert.sanitize_html(document, normalize_utf8=True))

    def test_preserves_intratable_header_references(self):
        document = ('<table><tr><th id="year">Year</th><th id="section">Section</th></tr>'
                    '<tr><td headers="year section" colspan="2">Data</td></tr></table>')
        table = parse(convert.sanitize_web_html(document)).find('body/div/table')
        ids = {n.get('id') for n in table.iter('th')}
        data = next(table.iter('td'))
        self.assertEqual(set(data.get('headers').split()), ids)
        self.assertEqual(data.get('colspan'), '2')

    def test_mhtml_explicit_root_preserves_selected_article_and_fragments(self):
        b = 'test-related-root'
        def source(start, second_id='main'):
            return (f'Content-Type: multipart/related; boundary="{b}"; type="text/html"; start="<{start}>"\r\n'
                    f'\r\n--{b}\r\nContent-Type: text/html; charset=utf-8\r\nContent-ID: <first>\r\n\r\n'
                    f'<p>Unchosen companion</p>\r\n--{b}\r\nContent-Type: text/html; charset=utf-8\r\nContent-ID: <{second_id}>\r\n'
                    f'\r\n<h1 id="heading">Main</h1><p><a href="#heading">Back</a></p>\r\n--{b}--\r\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.mht'; target=Path(tmp)/'document.html'
            path.write_bytes(source('main'))
            convert.mhtml_to_html(path, target, web_typography=True)
            doc=parse(target.read_text()).find('body')
            self.assertEqual(doc.find('h1').attrib['id'],'heading')
            self.assertEqual(doc.find('p/a').attrib['href'],'#heading')
            self.assertNotIn('Unchosen companion',target.read_text())
            convert.mhtml_to_html(path, target)
            self.assertIn('Unchosen companion',target.read_text())  # CHM path unchanged.
            for payload in (source('missing'),source('first',second_id='first')):
                path.write_bytes(payload)
                with self.assertRaisesRegex(ValueError,'unique HTML body'):
                    convert.mhtml_to_html(path, target, web_typography=True)

    def test_candidate_keeps_resource_and_active_content_boundaries(self):
        document = r'''<meta http-equiv="refresh" content="0;https://evil.test/">
        <meta name="viewport" content="user-scalable=no"><base href="https://evil.test/">
        <style>@import "https://evil.test/"; a {font-family: "A"; color: rgb(4, 5, 6);
        cursor: u\72l("https://evil.test/"), auto; width: expression(alert(1));}
        @font-face {src: url(https://evil.test/)} </style>
        <p align="bogus" onclick="run()" style="text-align:center; color:var(--x);
        width:expression(run()); background-color:url(https://evil.test/); position:fixed">text</p>
        <a href="&#106;avascript:run()">link</a><img src="https://evil.test/p" srcset="https://evil.test/x 2x">
        <link rel="stylesheet" href="//evil.test/">
        <svg><a xlink:href="javascript:run()">foreign</a></svg><iframe src="//evil.test/"></iframe>'''
        text = convert.sanitize_web_html(document)
        self.assertEqual(text.count('name="viewport"'), 1)
        self.assertIn('color: rgb(4, 5, 6);', text)
        self.assertIn('style="text-align:center;"', text)
        for forbidden in ('evil.test', 'user-scalable', 'onclick', 'expression', 'var(--x)', 'position:fixed',
                          'javascript:', 'align="bogus"', '<base', '<iframe', '<svg', '@font-face', '@import'):
            self.assertNotIn(forbidden, text)

    def test_producer_opt_in_is_immutable_and_only_changes_web_output(self):
        source = '<p align="center">中文</p><script>notProse()</script><pre>\n\n  a &lt; b</pre>'
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for ext in ('htm', 'html', 'mht', 'mhtml'):
                mime_source = (b'Content-Type: text/html; charset=utf-16\r\n'
                               b'Content-Transfer-Encoding: base64\r\n\r\n'
                               + base64.b64encode(source.encode('utf-16')))
                raw = source.encode() if ext in {'htm', 'html'} else mime_source
                item = {'repo': 'example/test', 'path': f'source.{ext}', 'extension': ext, 'revision': 'a'*40}
                with self.subTest(ext=ext), patch.object(pilot, 'download', side_effect=lambda u, p, t: p.write_bytes(raw)):
                    old, old_files = pilot.build_one(item, work / ext / 'baseline', None, pilot.TARGET_BUCKET)
                    new, files = pilot.build_one(item, work / ext / 'candidate', None, pilot.TARGET_BUCKET,
                                                 web_typography=True)
                    html = Path(files[new['object']]).read_bytes()
                    text = html.decode('utf-8')
                    self.assertNotIn('viewport', Path(old_files[old['object']]).read_text())
                    self.assertNotEqual(old['object'], new['object'])
                    self.assertEqual(old['source_sha256'], new['source_sha256'])
                    self.assertEqual(new['sha256'], hashlib.sha256(html).hexdigest())
                    self.assertEqual(json.loads(Path(files[new['manifest']]).read_text())['sha256'], new['sha256'])
                    self.assertIn('<p align="center">中文</p>', text)
                    self.assertNotIn('notProse', text)
                    self.assertEqual(parse(text).find('body/pre').text, '\n  a < b')

    def test_html_resources_use_elements_and_pinned_subtree(self):
        source = '''<base href="https://untrusted.test/page/"><link rel=stylesheet href=layout.css>
        <img src=picture.png alt=kept><img src="bad://host/a" data-src=picture.png alt=lazy>
        <img src=%2e%2e/escape.png><img src=%252e%252e/escape.png><img src=//untrusted.test/a.png>
        <pre>\n\n&lt;img src=example.png&gt;\t</pre>'''
        url = 'https://huggingface.co/datasets/example/data/resolve/' + 'a'*40 + '/folder/source.htm'
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp); inp=work/'source.htm'; inp.write_text(source)
            def fetch(address, target, **_):
                data = b'.chapter { color: #123; }' if address.endswith('.css') else b'test-image'
                target.write_bytes(data)
                return hashlib.sha256(data).hexdigest(), len(data)
            with patch.object(convert,'download_source', side_effect=fetch) as download:
                html=convert.inline_html_resources(inp,url,work,web_typography=True).read_text()
            root=parse(html)
            self.assertEqual([c.args[0] for c in download.call_args_list],
                             [url.rsplit('/',1)[0]+'/picture.png',url.rsplit('/',1)[0]+'/layout.css'])
            self.assertEqual([n.get('src') is not None for n in root.iter('img')],[True,True,False,False,False])
            self.assertEqual(root.find('body/pre').text,'\n<img src=example.png>\t')
            self.assertIsNone(root.find('head/base'))
            self.assertIn('.chapter{color: #123;',html)

    def test_portable_html_without_url_uses_candidate_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source, output = work / 'source.html', work / 'document.html'
            source.write_text('<h1>title</h1><script src="external.js">not prose</script><p align="left">text</p>')
            convert.convert_file({'extension': 'html'}, source, output, work, web_typography=True)
            self.assertIn('<p align="left">text</p>', output.read_text())
            self.assertNotIn('not prose', output.read_text())


if __name__ == '__main__':
    unittest.main()
