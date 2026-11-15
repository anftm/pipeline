import tempfile
import unittest
import zipfile
from lxml import etree
from ebooklib import epub
from pathlib import Path
from unittest.mock import patch
from scripts import chm_navigation, convert_reader_assets, recover_chm
from scripts.chm_navigation import SitemapParser, SourcePaths, flatten, navigation, repair_epub, source_text_mismatches


class ChmNavigationTests(unittest.TestCase):
    def test_source_text_gate_rejects_mojibake_in_mapped_chapters(self):
        from lxml import etree
        source_pages = [{"path": "About.html", "expected": "EtextWizard目录：一、关于Etext Wizard软件开发目的"}]
        mapping = {"About.html": ["EPUB/About.xhtml"]}
        corrupted = etree.fromstring(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b'EtextWizard\xc4\xbfl\xc2\xbc\xef\xbf\xbd\xef\xbf\xbd'
            b'\xe4\xb8\x80\xe3\x80\x81\xe5\x85\xb3\xe4\xba\x8eEtext Wizard'
            b'</body></html>'
        )
        mismatches = source_text_mismatches(source_pages, mapping, {"EPUB/About.xhtml": corrupted})
        self.assertEqual([row[0] for row in mismatches], ["About.html"])
        clean = etree.fromstring(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            'EtextWizard 目录：一、关于Etext Wizard 软件开发目的'
            '</body></html>'.encode()
        )
        self.assertEqual(source_text_mismatches(source_pages, mapping, {"EPUB/About.xhtml": clean}), [])

    def test_source_text_gate_rejects_omitted_unmapped_long_pages(self):
        from lxml import etree
        source_pages = [{"path": "appendix.txt", "expected": "正文内容" * 200}]
        self.assertEqual(source_text_mismatches(source_pages, {}, {}),
                         [("appendix.txt", len(source_pages[0]["expected"]), 0)])
        clean = etree.fromstring(("<html><body>" + source_pages[0]["expected"] + "</body></html>").encode())
        self.assertEqual(source_text_mismatches(source_pages, {}, {"EPUB/other.xhtml": clean}),
                         [("appendix.txt", len(source_pages[0]["expected"]), 0)])

    def test_source_text_gate_rejects_text_in_another_document(self):
        pages = [{"path": "a.htm", "expected": "正文甲"}]
        documents = {
            "a.xhtml": etree.fromstring('<html><body>正文乙</body></html>'.encode()),
            "b.xhtml": etree.fromstring('<html><body>正文甲</body></html>'.encode()),
        }
        self.assertEqual(source_text_mismatches(pages, {"a.htm": ["a.xhtml"]}, documents),
                         [("a.htm", 3, 3)])

    def test_source_text_gate_checks_only_the_mapped_section(self):
        pages = [{"path": "a.htm", "expected": "正文甲"}]
        document = etree.fromstring(
            '<html><body><section id="chm-page-0">正文乙</section>'
            '<section id="chm-page-1">正文甲</section></body></html>'.encode())
        for fragment, expected in [("chm-page-0", [("a.htm", 3, 3)]),
                                   ("missing", [("a.htm", 3, 0)]), ("chm-page-1", [])]:
            with self.subTest(fragment=fragment):
                self.assertEqual(source_text_mismatches(
                    pages, {"a.htm": ["book.xhtml#" + fragment]}, {"book.xhtml": document}), expected)

    def test_source_text_gate_rejects_omitted_short_unmapped_page(self):
        self.assertEqual(source_text_mismatches([{"path": "end.txt", "expected": "完"}], {}, {}),
                         [("end.txt", 1, 0)])

    @staticmethod
    def make_book(path, chapters, toc=None):
        book = epub.EpubBook()
        book.set_identifier('test'); book.set_title('Test'); book.set_language('zh')
        items = []
        for name, text in chapters:
            chapter = epub.EpubHtml(title=name, file_name=name, content=text)
            book.add_item(chapter); items.append(chapter)
        book.spine = items; book.toc = items if toc is None else toc
        book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
        epub.write_epub(str(path), book)

    def test_fallback_maps_mixed_source_types_using_generated_sections(self):
        from lxml import html as HTML
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            files = {
                'b.htm': '<p id="part">首章正文</p>',
                'z.htm': '<p>末章正文</p>',
                'a.txt': '短篇正文',
                'c.mht': 'MIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n<p>存档正文</p>',
                'toc.hhc': '<object><param name="Name" value="首章"><param name="Local" value="b.htm#part"></object>',
            }
            def extract(_source, root):
                for name, text in files.items():
                    (root / name).write_text(text, encoding='utf-8')
            def sevenzip(command, **kwargs):
                extract(None, Path(next(value[2:] for value in command if value.startswith('-o'))))
            merged = work / 'merged.html'
            with patch.object(convert_reader_assets, 'command_output', return_value=''), \
                    patch.object(convert_reader_assets.subprocess, 'run', side_effect=sevenzip):
                sections = convert_reader_assets.convert_chm_to_html(work / 'source.chm', merged, work)
            body = ''.join(HTML.tostring(node, encoding='unicode') for node in
                           HTML.document_fromstring(merged.read_text()).xpath('//section'))
            target = work / 'book.epub'
            self.make_book(target, [('merged.xhtml', body)])
            with patch.object(recover_chm, 'extract', side_effect=extract), \
                    patch.object(chm_navigation, 'repair_epub', wraps=repair_epub) as repair:
                chm_navigation.repair_conversion(work / 'source.chm', target, work, source_sections=sections)
            with zipfile.ZipFile(target) as archive:
                nav = etree.fromstring(archive.read('EPUB/nav.xhtml'))
                self.assertEqual(nav.xpath('//*[local-name()="a"]/@href'),
                                 ['merged.xhtml#chm-page-0--part'])
            self.assertEqual(repair.call_args.args[2], {
                'b.htm': ['EPUB/merged.xhtml#chm-page-0'],
                'z.htm': ['EPUB/merged.xhtml#chm-page-1'],
                'a.txt': ['EPUB/merged.xhtml#chm-page-2'],
                'c.mht': ['EPUB/merged.xhtml#chm-page-3'],
            })

    def test_conversion_passes_mapping_only_to_fallback_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp); source = work / 'source.chm'; target = work / 'book.epub'
            sections = {'a.htm': 'chm-page-0'}
            def convert(command, **kwargs):
                target.write_bytes(b'epub')
            with patch.object(convert_reader_assets, 'run_checked', side_effect=convert), \
                    patch.object(convert_reader_assets, 'sanitize_chm_epub'), \
                    patch.object(convert_reader_assets, 'validate_output'), \
                    patch.object(convert_reader_assets, 'validate_chm_epub'), \
                    patch.object(convert_reader_assets, 'convert_chm_to_html', return_value=sections), \
                    patch.object(chm_navigation, 'repair_conversion', side_effect=[ValueError('native text mismatch'), {}]) as repair:
                convert_reader_assets.convert_chm(source, target, work)
            self.assertEqual(repair.call_args_list[0].args, (source, target, work))
            self.assertEqual(repair.call_args_list[0].kwargs, {})
            self.assertEqual(repair.call_args_list[1].kwargs, {'source_sections': sections})

    def test_native_conversion_does_not_map_short_text_inside_another_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            def extract(_source, root):
                (root / 'end.txt').write_text('完')
                (root / 'other.htm').write_text('<p>完全不同的正文</p>')
            target = work / 'book.epub'
            self.make_book(target, [('other.htm', '<p>完全不同的正文</p>')])
            with patch.object(recover_chm, 'extract', side_effect=extract), \
                    self.assertRaisesRegex(ValueError, 'end.txt'):
                chm_navigation.repair_conversion(work / 'source.chm', target, work)

    def test_native_conversion_maps_renamed_short_pages_and_split_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            def extract(_source, root):
                (root / 'chapter.htm').write_text('<p>前半正文后半正文</p>')
                (root / 'end.txt').write_text('完')
            target = work / 'book.epub'
            self.make_book(target, [('chapter_split_000.htm', '<p>前半正文</p>'),
                                    ('chapter_split_001.htm', '<p>后半正文</p>'),
                                    ('renamed.xhtml', '<p>完</p>')])
            with patch.object(recover_chm, 'extract', side_effect=extract), \
                    patch.object(chm_navigation, 'repair_epub', return_value={}) as repair:
                chm_navigation.repair_conversion(work / 'source.chm', target, work)
            self.assertEqual(repair.call_args.args[2], {
                'chapter.htm': ['EPUB/chapter_split_000.htm', 'EPUB/chapter_split_001.htm'],
                'end.txt': ['EPUB/renamed.xhtml'],
            })

    def test_native_conversion_keeps_source_missing_images_as_reported_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            def extract(_source, root):
                (root / 'page.htm').write_text('<p>正文</p><img src="missing.png">')
            target = work / 'book.epub'
            self.make_book(target, [('page.htm', '<p>正文</p><img src="missing.png"/>')])
            with patch.object(recover_chm, 'extract', side_effect=extract):
                report = chm_navigation.repair_conversion(work / 'source.chm', target, work)
            self.assertEqual(report['images']['missing'], [
                {'page': 'EPUB/page.htm', 'src': 'missing.png', 'reason': 'missing_source'}])

    def test_explicit_section_mapping_rejects_missing_and_wrong_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            def extract(_source, root):
                (root / 'a.htm').write_text('<p>正文甲</p>')
                (root / 'b.htm').write_text('<p>正文乙</p>')
            target = work / 'book.epub'
            self.make_book(target, [('merged.xhtml', '<section id="chm-page-0">正文甲</section>'
                                     '<section id="chm-page-1">正文乙</section>')])
            for section in ['chm-page-1', 'chm-page-9']:
                with self.subTest(section=section), \
                        patch.object(recover_chm, 'extract', side_effect=extract), \
                        self.assertRaisesRegex(ValueError, 'converted CHM'):
                    chm_navigation.repair_conversion(work / 'source.chm', target, work,
                        source_sections={'a.htm': section, 'b.htm': 'chm-page-1'})

    def test_inference_rejects_two_sources_sharing_one_output(self):
        from PIL import Image
        # Exercise filename+text, two text inferences, two NCX labels, and
        # overlapping ordinary-fragment/whole-document mappings.
        for mode in ['filename', 'text', 'labels', 'overlap']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                def extract(_source, root):
                    for name, color in [('a', 'red'), ('b', 'blue')]:
                        (root / (name + '.htm')).write_text(f'<p>相同正文</p><img src="{name}.png">')
                        Image.new('RGB', (2, 2), color).save(root / (name + '.png'))
                    if mode in ['labels', 'overlap']:
                        (root / 'toc.hhc').write_text(''.join(
                            f'<object><param name="Name" value="{name}"><param name="Local" value="{name}.htm"></object>'
                            for name in ['a', 'b']))
                member = 'a.htm' if mode == 'filename' else 'merged.xhtml'
                toc = [epub.Link(member + ('#chapter' if mode == 'overlap' and name == 'a' else ''), name, name)
                       for name in ['a', 'b']]
                target = work / 'book.epub'
                self.make_book(target, [(member, '<div id="chapter"><p>相同正文</p><img src="a.png"/></div>')], toc)
                with patch.object(recover_chm, 'extract', side_effect=extract), \
                        self.assertRaisesRegex(ValueError, 'CHM'):
                    chm_navigation.repair_conversion(work / 'source.chm', target, work)

    def test_mapping_rejects_split_filename_alias_and_duplicate_explicit_section(self):
        for explicit in [False, True]:
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                def extract(_source, root):
                    for name in ['chapter.htm', 'chapter_split_000.htm']:
                        (root / name).write_text('<p>相同正文</p>')
                target = work / 'book.epub'
                self.make_book(target, [('chapter_split_000.htm', '<section id="chm-page-0">相同正文</section>')])
                sections = ({name: 'chm-page-0' for name in ['chapter.htm', 'chapter_split_000.htm']}
                            if explicit else None)
                with patch.object(recover_chm, 'extract', side_effect=extract), \
                        self.assertRaisesRegex(ValueError, 'CHM target reuse'):
                    chm_navigation.repair_conversion(work / 'source.chm', target, work, source_sections=sections)

    def test_distinct_boundaries_allow_identical_text_from_distinct_sources(self):
        for explicit in [False, True]:
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                def extract(_source, root):
                    for name in ['a', 'b']:
                        (root / (name + '.htm')).write_text('<p>相同正文</p>')
                    (root / 'toc.hhc').write_text(''.join(
                        f'<object><param name="Name" value="{name}"><param name="Local" value="{name}.htm"></object>'
                        for name in ['a', 'b']))
                ids = ['chm-page-0', 'chm-page-1'] if explicit else ['first', 'later']
                target = work / 'book.epub'
                self.make_book(target, [('merged.xhtml', ''.join(
                    f'<section id="{fragment}">相同正文</section>' for fragment in ids))],
                    [epub.Link('merged.xhtml#' + fragment, name, name) for name, fragment in zip(['a', 'b'], ids)])
                sections = dict(zip(['a.htm', 'b.htm'], ids)) if explicit else None
                with patch.object(recover_chm, 'extract', side_effect=extract):
                    chm_navigation.repair_conversion(work / 'source.chm', target, work, source_sections=sections)
                with zipfile.ZipFile(target) as archive:
                    nav = etree.fromstring(archive.read('EPUB/nav.xhtml'))
                    self.assertEqual(nav.xpath('//*[local-name()="a"]/@href'),
                                     ['merged.xhtml#' + fragment for fragment in ids])

    def test_wrong_ncx_fragment_cannot_fall_back_to_matching_whole_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            def extract(_source, root):
                (root / 'a.htm').write_text('<p>完整正文甲</p>')
                (root / 'toc.hhc').write_text('<object><param name="Name" value="Chapter">'
                                            '<param name="Local" value="a.htm"></object>')
            target = work / 'book.epub'
            self.make_book(target, [('merged.xhtml', '<a id="wrong"></a><p>完整正文甲</p>')],
                           [epub.Link('merged.xhtml#wrong', 'Chapter', 'chapter')])
            with patch.object(recover_chm, 'extract', side_effect=extract), \
                    self.assertRaisesRegex(ValueError, 'CHM chapter boundary'):
                chm_navigation.repair_conversion(work / 'source.chm', target, work)

    def test_ncx_ordinary_fragment_requires_verified_chapter_text(self):
        for fragment, valid in [('right', True), ('wrong', False), ('missing', False), ('heading', False)]:
            with self.subTest(fragment=fragment), tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                def extract(_source, root):
                    (root / 'a.htm').write_text('<p>完整正文甲</p>')
                    (root / 'toc.hhc').write_text('<object><param name="Name" value="Chapter">'
                                                '<param name="Local" value="a.htm"></object>')
                target = work / 'book.epub'
                self.make_book(target, [('merged.xhtml', '<h1 id="heading">标题</h1>'
                    '<div id="right">完整正文甲</div><div id="wrong">其他正文乙</div>')],
                    [epub.Link('merged.xhtml#' + fragment, 'Chapter', 'chapter')])
                with patch.object(recover_chm, 'extract', side_effect=extract):
                    if valid:
                        chm_navigation.repair_conversion(work / 'source.chm', target, work)
                        with zipfile.ZipFile(target) as archive:
                            nav = etree.fromstring(archive.read('EPUB/nav.xhtml'))
                            self.assertEqual(nav.xpath('//*[local-name()="a"]/@href'), ['merged.xhtml#right'])
                    else:
                        with self.assertRaisesRegex(ValueError, 'CHM'):
                            chm_navigation.repair_conversion(work / 'source.chm', target, work)

    def test_missing_source_anchor_keeps_later_section_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp); source = work / 'source'; source.mkdir()
            (source / 'b.htm').write_text('<p>后章正文</p>')
            (source / 'toc.hhc').write_text('<object><param name="Name" value="Later">'
                                          '<param name="Local" value="b.htm#absent"></object>')
            target = work / 'book.epub'
            self.make_book(target, [('merged.xhtml', '<section id="chm-page-0">首章正文</section>'
                                    '<section id="chm-page-1">后章正文</section>')])
            report = repair_epub(target, source, {'b.htm': ['EPUB/merged.xhtml#chm-page-1']})
            self.assertEqual(report['source_issues'], [{'kind': 'missing_source_anchor', 'href': 'b.htm#absent'}])
            with zipfile.ZipFile(target) as archive:
                for member, query in [('EPUB/nav.xhtml', '//*[local-name()="a"]/@href'),
                                      ('EPUB/toc.ncx', '//*[local-name()="content"]/@src')]:
                    self.assertEqual(etree.fromstring(archive.read(member)).xpath(query),
                                     ['merged.xhtml#chm-page-1'])

    def test_chm_keeps_native_tree_instead_of_flat_chapter_sidecar(self):
        from scripts.reader_assets import needs_epub_chapters
        self.assertTrue(needs_epub_chapters('chm','epub',1))

    def test_repaired_encoded_anchor_and_linked_full_size_image(self):
        from scripts.chm_resources import repair_images, repair_links
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            (source/'page.htm').write_text('<a name="中文">Text</a><a href="#中文">Jump</a><a href="PIC.PNG">Image</a>')
            Image.new('RGB',(4,4),'red').save(source/'pic.png')
            book=epub.EpubBook();book.set_identifier('test');book.set_title('Test');book.set_language('en')
            chapter=epub.EpubHtml(title='Page',file_name='page.xhtml',content='<p id="%E4%B8%AD%E6%96%87">Text</p><a href="#%E4%B8%AD%E6%96%87">Jump</a><a href="PIC.PNG">Image</a>')
            book.add_item(chapter);book.spine=[chapter];book.toc=[chapter];book.add_item(epub.EpubNcx());book.add_item(epub.EpubNav())
            path=root/'book.epub';epub.write_epub(str(path),book)
            mapping={'page.htm':['EPUB/page.xhtml']}
            self.assertEqual(repair_images(path,source,mapping)['added'],1)
            self.assertFalse(repair_links(path,source,mapping)['missing'])
            with zipfile.ZipFile(path) as z:
                doc=etree.fromstring(z.read('EPUB/page.xhtml'))
                links=doc.xpath('//*[local-name()="a"]/@href')
                self.assertEqual(links[0],'page.xhtml#%25E4%25B8%25AD%25E6%2596%2587')
                self.assertTrue(links[1].startswith('chm-images/'))

    def test_static_menu_restores_groups_without_evaluating_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'js').mkdir();(root/'txt').mkdir()
            (root/'js/page.js').write_text("var pages=[]; pages[0]=['a','(1)','2','First']; pages[1]=['b','(2)','2']; throw Error('must not execute');")
            for name in ['a','b']:(root/'txt'/ (name+'.txt')).write_text('body')
            result,_=navigation(root)
            self.assertEqual([(n['label'],d) for n,d in flatten(result['nodes'])],[('First',0),('(1)',1),('(2)',1)])
            self.assertEqual(result['nodes'][0]['children'][1]['source'],'txt/b.txt')

    def test_script_chapter_preserves_declared_heading_and_body(self):
        from scripts.chm_static_navigation import script_page
        title,body=script_page('var makechm_a_title="Heading";var makechm_a_content="<p>Body</p>";'
            'document.write("<h1>"+makechm_a_title+"</h1>");document.write(makechm_a_content);')
        self.assertEqual((title,body),('Heading','<h1>Heading</h1><p>Body</p>'))
        with self.assertRaises(ValueError):
            script_page('var makechm_a_content="Body";document.write(fetch("https://example.org"));')

    def test_html_contents_preserves_all_targets_beyond_fifty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'index.html').write_text('<center>上卷 正文</center>'+''.join(f'<a href="{i}.htm">标题{i}</a>' for i in range(65)))
            for i in range(65):(root/f'{i}.htm').write_text('body')
            result,_=navigation(root)
            self.assertEqual(len(result['nodes'][0]['children']),65)
            self.assertEqual(result['nodes'][0]['children'][-1]['label'],'标题64')

    def test_groups_hierarchy_apostrophes_and_repeated_anchor_targets(self):
        parser = SitemapParser()
        parser.feed('''<object type="text/site properties"><param name="Name" value="ignored"></object>
          <ul><li><object><param name="Name" value="Group's title"></object><ul>
          <li><object><param name="Name" value="One &amp; two"><param name="Local" value="a.htm#one"></object>
          <li><object><param name="Name" value="Three"><param name="Local" value="a.htm#two"></object>
          </ul><li><object><param name="Name" value="End"><param name="Local" value="b.htm"></object></ul>''')
        self.assertEqual([(n['label'],d) for n,d in flatten(parser.nodes)],
                         [("Group's title",0),('One & two',1),('Three',1),('End',0)])
        self.assertEqual([n['href'] for n,d in flatten(parser.nodes)], ['', 'a.htm#one', 'a.htm#two','b.htm'])

    def test_windows_paths_percent_encoding_and_ambiguity(self):
        paths=SourcePaths(['a/b#c.htm','images/X.png','A.htm','a.HTM'])
        self.assertEqual(paths.resolve('a/toc.hhc','b%23c.htm#section'),('a/b#c.htm','section','exact'))
        self.assertEqual(paths.resolve('a/page.htm','..\\Images\\x.png'),('images/X.png','','casefold'))
        self.assertEqual(paths.resolve('','a.htm')[2],'ambiguous')
        self.assertEqual(paths.resolve('','../../escape')[2],'outside')

    def test_multiple_distinct_hhc_trees_are_not_silently_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ['one','two']:
                (root/(name+'.hhc')).write_text(f'<object><param name="Name" value="{name}"></object>')
            with self.assertRaisesRegex(ValueError,'multiple source HHC'):
                navigation(root)

    def test_repair_keeps_chapter_bytes_and_restores_source_tree_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source';source.mkdir()
            (source/'toc.hhc').write_text('<ul><li><object><param name="Name" value="Volume"></object><ul>'
                '<li><object><param name="Name" value="Original Two"><param name="Local" value="two.htm#part"></object>'
                '<li><object><param name="Name" value="Original One"><param name="Local" value="one.htm"></object></ul></ul>')
            for name in ['one','two']:(source/(name+'.htm')).write_text('<p id="part">'+name+'</p>')
            book=epub.EpubBook();book.set_identifier('test');book.set_title('Test');book.set_language('en')
            chapters=[]
            for name in ['one','two']:
                c=epub.EpubHtml(title='Wrong '+name,file_name=name+'.xhtml',content='<p id="part">'+name+'</p>')
                book.add_item(c);chapters.append(c)
            book.toc=chapters;book.spine=chapters;book.add_item(epub.EpubNcx());book.add_item(epub.EpubNav())
            path=root/'book.epub';epub.write_epub(str(path),book)
            with zipfile.ZipFile(path) as archive:
                original={n:archive.read(n) for n in ['EPUB/one.xhtml','EPUB/two.xhtml']}
            report=repair_epub(path,source,{'one.htm':['EPUB/one.xhtml'],'two.htm':['EPUB/two.xhtml']})
            self.assertFalse(report['spine_preserved'])
            with zipfile.ZipFile(path) as archive:
                for name,data in original.items():self.assertEqual(archive.read(name),data)
                nav=etree.fromstring(archive.read('EPUB/nav.xhtml'))
                self.assertEqual(nav.xpath('//*[local-name()="span"]/text()'),['Volume'])
                self.assertEqual(nav.xpath('//*[local-name()="a"]/@href'),['two.xhtml#part','one.xhtml'])
            self.assertEqual(report['spine'],['EPUB/two.xhtml','EPUB/one.xhtml'])
