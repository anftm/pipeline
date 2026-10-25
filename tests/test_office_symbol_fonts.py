from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lxml import etree
from scripts import office_symbol_fonts as symbols


class OfficeSymbolFontsTests(unittest.TestCase):
    def test_exact_bullet_mapping_preserves_text_and_original_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)
            source=work/'source.pptx'
            raw=(f'<a:root xmlns:a="{symbols.NS}"><a:pPr><a:buFont typeface="Wingdings 3" charset="2"/>'
                 '<a:buChar char="&#xF0B4;"/></a:pPr><a:t>原始正文\uf0b4</a:t>'
                 '<a:r><a:rPr><a:sym typeface="Wingdings" charset="2"/></a:rPr><a:t>&#xF0E0; </a:t></a:r>'
                 '<a:pPr><a:buFont typeface="Unknown"/><a:buChar char="&#xF0B4;"/></a:pPr></a:root>').encode()
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('ppt/slideMasters/slideMaster1.xml',raw)
                archive.writestr('ppt/media/image1.png',b'original image')
            original=source.read_bytes()
            with patch.object(symbols,'installed_font',return_value=work/'font.ttf'):
                target,report=symbols.prepare(source,work)
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(report['changes'][0]['original_xml'],raw.decode())
            with zipfile.ZipFile(target) as archive:
                root=etree.fromstring(archive.read('ppt/slideMasters/slideMaster1.xml'))
                ns={'a':symbols.NS}
                self.assertEqual([n.get('char') for n in root.findall('.//a:buChar',ns)],['\U0001f836','\uf0b4'])
                self.assertEqual(root.find('a:t',ns).text,'原始正文\uf0b4')
                self.assertEqual(root.find('.//a:r/a:t',ns).text,'\U0001f86a ')
                self.assertEqual(archive.read('ppt/media/image1.png'),b'original image')
