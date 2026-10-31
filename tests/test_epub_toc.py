import tempfile
import gzip
import json
import unittest
import zipfile
from pathlib import Path

from scripts import epub_chapters, reader_assets


class EpubTocTests(unittest.TestCase):
    def test_search_indexes_visible_literal_references_without_second_decode(self):
        document = ('<html:html xmlns:html="http://www.w3.org/1999/xhtml">'
                    '<html:head><html:title>标题</html:title></html:head><html:body>'
                    '<html:p>保留 &amp;#0 和 &amp;nbsp; 与 &amp;amp; &amp;lt;b&amp;gt;</html:p>'
                    '<html:style>不能检索样式</html:style><html:noscript>不能检索备用脚本</html:noscript>'
                    '<html:p>真实空格&#160;尾部</html:p></html:body></html:html>')
        self.assertEqual(epub_chapters._chapter_text(document),
                         '标题 保留 &#0 和 &nbsp; 与 &amp; &lt;b&gt; 真实空格 尾部')
        self.assertEqual(epub_chapters._document_title(document), '标题')

    def test_bundle_text_title_and_search_use_one_entity_decode_from_served_xhtml(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);source = root/'literal.epub';output = root/'bundle'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
                archive.writestr('content.opf', '<package><manifest><item id="a" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/></spine></package>')
                archive.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Literal &amp;nbsp;</title></head><body><p>Visible &amp;#0 &amp;nbsp;</p></body></html>')
            manifest = epub_chapters.build_bundle(source, output)
            index = json.loads(gzip.decompress((output/'epub-search-index.json.gz').read_bytes()))
            # Existing title precedence prefers the first readable body block.
            self.assertEqual(manifest['chapters'][0]['title'], 'Visible &#0 &nbsp;')
            self.assertEqual(index['text_profile'], 'dom-text-v1')
            self.assertEqual(index['chapters'][0]['text'], 'Literal &nbsp; Visible &#0 &nbsp;')

    def test_cover_only_navigation_is_rebuilt_from_readable_chapters(self):
        records = [{"index": 1, "source_path": "titlepage.xhtml", "title": "Start", "clean": "<html/>"},
                   {"index": 2, "source_path": "book.xhtml", "title": "第一章 起点", "clean": "<html/>"},
                   {"index": 3, "source_path": "book2.xhtml", "title": "第二章 终点", "clean": "<html/>"}]
        toc = epub_chapters.bundle_toc(
            [{"title": "Start", "source_path": "titlepage.xhtml", "fragment": "", "depth": 0}], records)
        self.assertEqual([item["chapter"] for item in toc], [2, 3])
        self.assertEqual([item["title"] for item in toc], ["第一章 起点", "第二章 终点"])

    def test_real_navigation_is_kept_even_when_it_starts_at_the_cover(self):
        records = [{"index": 1, "source_path": "titlepage.xhtml", "title": "Cover", "clean": "<html/>"},
                   {"index": 2, "source_path": "a.xhtml", "title": "一", "clean": "<html/>"},
                   {"index": 3, "source_path": "b.xhtml", "title": "二", "clean": "<html/>"}]
        entries = [{"title": "Cover", "source_path": "titlepage.xhtml", "fragment": "", "depth": 0},
                   {"title": "一", "source_path": "a.xhtml", "fragment": "", "depth": 0},
                   {"title": "二", "source_path": "b.xhtml", "fragment": "", "depth": 0}]
        toc = epub_chapters.bundle_toc(entries, records)
        self.assertEqual([item["chapter"] for item in toc], [1, 2, 3])

    def test_written_search_profile_and_complete_text_are_checksum_bound(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = b'<html><body><p>literal &amp;nbsp; tail</p></body></html>'
            path = root / 'chapters/chapter-0001.xhtml'
            path.parent.mkdir();path.write_bytes(body)
            chapters = [dict(index=1, title='literal', path='chapters/chapter-0001.xhtml',
                             bytes=len(body), sha256=hashlib.sha256(body).hexdigest())]
            ref = epub_chapters.write_search_index(root, chapters)
            raw = (root / ref['path']).read_bytes()
            data = json.loads(gzip.decompress(raw))
            self.assertEqual(ref['text_profile'], 'dom-text-v1')
            self.assertEqual(data['text_profile'], ref['text_profile'])
            self.assertEqual(data['chapters'][0]['text'], 'literal &nbsp; tail')
            self.assertEqual(ref['sha256'], hashlib.sha256(raw).hexdigest())
            path.write_bytes(body + b'changed')
            with self.assertRaisesRegex(ValueError, 'chapter bytes differ'):
                epub_chapters.write_search_index(root, chapters)

    def build(self, navigation, *, nav=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'book.epub'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="O/content.opf"/></rootfiles></container>')
            item = ('<item id="toc" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>' if nav else
                    '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
            archive.writestr('O/content.opf', '<package><manifest>' + item + '<item id="a" href="book.xhtml" media-type="application/xhtml+xml"/><item id="b" href="split.xhtml" media-type="application/xhtml+xml"/></manifest><spine toc="toc"><itemref idref="a"/><itemref idref="b"/></spine></package>')
            archive.writestr('O/nav.xhtml' if nav else 'O/toc.ncx', navigation)
            archive.writestr('O/book.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Unknown Text</title></head><body><h1 id="卷一">第一卷</h1><p>前文</p><h2 id="day">一月三日</h2><p>正文</p></body></html>')
            archive.writestr('O/split.xhtml', '<html><body><p>没有独立目录项的续页正文</p></body></html>')
        return epub_chapters.build_bundle(source, root / 'bundle')

    def test_ncx_keeps_nested_targets_in_same_file_and_recovers_unknown_label(self):
        result = self.build('<ncx><navMap><navPoint><navLabel><text>第一卷</text></navLabel><content src="book.xhtml#%E5%8D%B7%E4%B8%80"/><navPoint><navLabel><text>Unknown Text</text></navLabel><content src="book.xhtml#day"/></navPoint></navPoint></navMap></ncx>')
        self.assertEqual(result['toc'], [
            dict(title='第一卷', chapter=1, fragment='卷一', depth=0),
            dict(title='一月三日', chapter=1, fragment='day', depth=1)])
        self.assertEqual(len(result['chapters']), 2)

    def test_epub3_uses_toc_only_and_keeps_unlinked_group(self):
        result = self.build('<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="landmarks"><ol><li><a href="split.xhtml">正文</a></li></ol></nav><nav epub:type="toc"><ol><li><span>日记</span><ol><li><a href="book.xhtml#day">一月三日</a></li></ol></li></ol></nav></body></html>', nav=True)
        self.assertEqual(result['toc'], [dict(title='日记', chapter=1, fragment='day', depth=0),
                                         dict(title='一月三日', chapter=1, fragment='day', depth=1)])

    def test_unrecoverable_navigation_uses_a_stable_fallback_title(self):
        result = self.build('<ncx><navMap><navPoint><navLabel><text>Unknown Text</text></navLabel><content src="book.xhtml#missing"/></navPoint></navMap></ncx>')
        self.assertEqual(result['toc'][0]['title'], '章节 1')

    def test_missing_navigation_fragment_uses_the_matching_heading(self):
        # The source anchor is gone, but the label names a heading in this chapter, so the
        # directory can still land on it instead of the chapter start.
        result = self.build('<ncx><navMap><navPoint><navLabel><text>第一卷</text></navLabel><content src="book.xhtml#missing"/></navPoint></navMap></ncx>')
        self.assertEqual(result['toc'], [dict(title='第一卷', chapter=1, fragment='卷一', depth=0)])

    def test_unresolvable_navigation_fragment_falls_back_to_chapter_start(self):
        result = self.build('<ncx><navMap><navPoint><navLabel><text>不相干的标题</text></navLabel><content src="book.xhtml#missing"/></navPoint></navMap></ncx>')
        self.assertEqual(result['toc'], [dict(title='不相干的标题', chapter=1, fragment='', depth=0)])

    def test_shared_footnote_anchor_is_replaced_by_the_named_heading(self):
        # Several entries pointing at one in-page footnote anchor (duokan "note1") must land
        # on their own heading, not on the first verse that owns that anchor.
        navigation = ('<ncx><navMap>'
                      '<navPoint><navLabel><text>一月三日</text></navLabel><content src="book.xhtml#day"/></navPoint>'
                      '<navPoint><navLabel><text>第二卷</text></navLabel><content src="book.xhtml#day"/></navPoint>'
                      '</navMap></ncx>')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'book.epub'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="O/content.opf"/></rootfiles></container>')
            archive.writestr('O/content.opf', '<package><manifest><item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="a" href="book.xhtml" media-type="application/xhtml+xml"/></manifest><spine toc="toc"><itemref idref="a"/></spine></package>')
            archive.writestr('O/toc.ncx', navigation)
            archive.writestr('O/book.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                                            '<h1 id="day">一月三日</h1><p>正文</p>'
                                            '<h1 id="two">第二卷</h1><p>后文</p></body></html>')
        result = epub_chapters.build_bundle(source, root / 'bundle')
        self.assertEqual(result['toc'], [
            dict(title='一月三日', chapter=1, fragment='day', depth=0),
            dict(title='第二卷', chapter=1, fragment='two', depth=0)])

    def test_navigation_entry_moves_to_the_chapter_that_owns_its_label(self):
        # A converted MOBI can point every entry at one footnote anchor in the wrong
        # document; the label must decide the destination.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'book.epub'
            navigation = ('<ncx><navMap>'
                          '<navPoint><navLabel><text>第二首</text></navLabel><content src="one.xhtml#note1"/></navPoint>'
                          '<navPoint><navLabel><text>第一首</text></navLabel><content src="one.xhtml#note1"/></navPoint>'
                          '</navMap></ncx>')
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
                archive.writestr('content.opf', '<package><manifest><item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="a" href="one.xhtml" media-type="application/xhtml+xml"/><item id="b" href="two.xhtml" media-type="application/xhtml+xml"/></manifest><spine toc="toc"><itemref idref="a"/><itemref idref="b"/></spine></package>')
                archive.writestr('toc.ncx', navigation)
                archive.writestr('one.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                                              '<h1 id="note1">第一首</h1><p>甲</p></body></html>')
                archive.writestr('two.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                                              '<h1 title="第二首" id="t2"></h1><p>乙</p></body></html>')
            result = epub_chapters.build_bundle(source, root / 'bundle')
            self.assertEqual([(item['title'], item['chapter']) for item in result['toc']],
                             [('第二首', 2), ('第一首', 1)])

    def test_merged_chm_relocation_preserves_same_document_source_anchor(self):
        # The title may occur far inside a merged file. Rediscovering the same
        # document must not discard its independently valid page destination.
        clean=('<html><body><section id="chm-page-4"><h1 title="抗日战争胜利后的新形势">'
               '</h1><p>正文</p></section></body></html>')
        records=[{'index':1,'source_path':'merged.xhtml','clean':clean,'title':'合并正文'}]
        entries=[{'title':'抗日战争胜利后的新形势','source_path':'merged.xhtml',
                  'fragment':'chm-page-4','depth':0}]
        # Trigger the relocation branch as for imperfect heading detection,
        # while still resolving the same physical source document.
        from unittest.mock import patch
        with patch.object(epub_chapters,'_label_matches',side_effect=[False,True]):
            relocated=epub_chapters.relocate_toc_entries(entries,records)
        self.assertEqual(relocated[0]['fragment'],'chm-page-4')
        self.assertEqual(epub_chapters.bundle_toc(relocated,records)[0]['fragment'],'chm-page-4')

    def test_self_closing_heading_title_is_read(self):
        self.assertEqual(epub_chapters._chapter_headings('<html:body><html:h1 title="第一首" id="a" />'
                                                         '<html:p>正文</html:p></html:body>'), ['第一首'])

    def test_manifest_rejects_bad_navigation_target(self):
        result = self.build('<ncx><navMap/></ncx>')
        result['toc'] = [dict(title='错误', chapter=3, depth=0, fragment='')]
        with self.assertRaisesRegex(ValueError, 'TOC entry'):
            reader_assets.validate_chapter_manifest(result)

    def test_plain_directory_paragraphs_link_to_matching_chapters(self):
        navigation = '<ncx><navMap><navPoint><navLabel><text>第一篇</text></navLabel><content src="book.xhtml"/></navPoint><navPoint><navLabel><text>第二篇</text></navLabel><content src="split.xhtml"/></navPoint></navMap></ncx>'
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'book.epub'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="O/content.opf"/></rootfiles></container>')
            archive.writestr('O/content.opf', '<package><manifest><item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="a" href="book.xhtml" media-type="application/xhtml+xml"/><item id="b" href="split.xhtml" media-type="application/xhtml+xml"/></manifest><spine toc="toc"><itemref idref="a"/><itemref idref="b"/></spine></package>')
            archive.writestr('O/toc.ncx', navigation)
            archive.writestr('O/book.xhtml', '<html><body><h1>目录</h1><p>第一篇</p><p>第二篇</p><p>没有匹配</p></body></html>')
            archive.writestr('O/split.xhtml', '<html><body><h1>第二篇</h1><p>正文</p></body></html>')
        bundle = root / 'bundle'
        epub_chapters.build_bundle(source, bundle)
        text = (bundle / 'chapters/chapter-0001.xhtml').read_text(encoding='utf-8')
        self.assertIn('href="chapter-0001.xhtml"', text)
        self.assertIn('href="chapter-0002.xhtml"', text)
        self.assertIn('>没有匹配</', text)

    def test_unsafe_epub_paths_are_omitted_without_losing_valid_spine_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "unsafe-paths.epub"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(
                    "META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="O/content.opf"/></rootfiles></container>')
                archive.writestr(
                    "O/content.opf",
                    '<package><manifest>'
                    '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                    '<item id="outside" href="../../outside.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="book" href="book.xhtml" media-type="application/xhtml+xml"/>'
                    '</manifest><spine><itemref idref="outside"/><itemref idref="book"/></spine></package>')
                archive.writestr(
                    "O/toc.ncx",
                    '<ncx><navMap><navPoint><navLabel><text>Bad</text></navLabel>'
                    '<content src="../../outside.xhtml"/></navPoint></navMap></ncx>')
                archive.writestr(
                    "O/book.xhtml",
                    '<html><body><p>保留的正文</p><img src="../../../../outside.png"/>'
                    '<a href="../../../../outside.xhtml">越界链接</a></body></html>')
                archive.writestr("../outside.xhtml", "<html><body>must not read</body></html>")
                archive.writestr("../../outside.png", b"must not copy")

            bundle = root / "bundle"
            result = epub_chapters.build_bundle(source, bundle)
            self.assertEqual(len(result["chapters"]), 1)
            chapter = (bundle / result["chapters"][0]["path"]).read_text(encoding="utf-8")
            self.assertIn("保留的正文", chapter)
            self.assertNotIn("src=", chapter)
            self.assertNotIn("outside.xhtml", chapter)
            self.assertNotIn("must not read", chapter)
            self.assertFalse((bundle / "resources").exists())


if __name__ == '__main__':
    unittest.main()
