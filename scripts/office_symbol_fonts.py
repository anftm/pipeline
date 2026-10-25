"""Resolve explicitly declared legacy Office symbols before PDF conversion."""
from pathlib import Path
import hashlib
import json
import os
import tempfile
import urllib.request
import zipfile

PROFILE = 'office-symbol-fonts-v2'
FONT_REVISION = 'ffebf8c1ee449e544955a7e813c54f9b73848eac'
FONT_SHA256 = '630846d528dbe4c4981370a4d0a9475a1fd1491a129bb411f8e157cdb5de13c6'
FONT_BYTES = 656852
FONT_NAME = 'Noto Sans Symbols2'
FONT_URL = f'https://raw.githubusercontent.com/notofonts/noto-fonts/{FONT_REVISION}/hinted/ttf/NotoSansSymbols2/NotoSansSymbols2-Regular.ttf'
MAPPING_REFERENCE = 'https://www.alanwood.net/demos/wingdings-3.html'
# Wingdings 3 byte B4, PostScript glyph i8right. No inference from the PUA
# codepoint alone: both the explicitly declared font and glyph must match.
BULLETS = {('Wingdings 3', '\uf0b4'): '\U0001f836'}
RUN_SYMBOLS = {('Wingdings', '\uf0e0'): '\U0001f86a'}
NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'


def installed_font():
    directory = Path(os.environ.get('READER_SYMBOL_FONT_DIR', str(Path.home()/'.local/share/fonts/reader-symbols')))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/'NotoSansSymbols2-Regular.ttf'
    if not path.exists():
        with urllib.request.urlopen(FONT_URL, timeout=90) as response:
            raw = response.read(FONT_BYTES + 1)
        if len(raw) != FONT_BYTES or hashlib.sha256(raw).hexdigest() != FONT_SHA256:
            raise ValueError('symbol font checksum mismatch')
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            pending = Path(stream.name)
            try:
                stream.write(raw)
                stream.flush()
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
    raw = path.read_bytes()
    if len(raw) != FONT_BYTES or hashlib.sha256(raw).hexdigest() != FONT_SHA256:
        raise ValueError('symbol font checksum mismatch')
    return path


def prepare(source, work):
    """Rewrite a conversion copy, retaining original XML/font/glyph evidence."""
    from lxml import etree
    if not zipfile.is_zipfile(source):
        return source, None
    changes, replacements = [], {}
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if not info.filename.startswith('ppt/') or not info.filename.endswith('.xml'):
                continue
            raw = archive.read(info)
            if b'Wingdings' not in raw:
                continue
            root = etree.fromstring(raw, parser)
            found = []
            for node in root.iter('{' + NS + '}buChar'):
                font = node.getparent().find('{' + NS + '}buFont')
                if font is None:
                    continue
                key = (font.get('typeface'), node.get('char'))
                if key not in BULLETS:
                    continue
                found.append({'font': dict(font.attrib), 'char': node.get('char'), 'unicode': BULLETS[key]})
                node.set('char', BULLETS[key])
                font.set('typeface', FONT_NAME)
                font.attrib.pop('charset', None)
            for run in root.iter('{' + NS + '}r'):
                properties = run.find('{' + NS + '}rPr')
                node = run.find('{' + NS + '}t')
                if properties is None or node is None or not node.text:
                    continue
                font = properties.find('{' + NS + '}sym')
                if font is None:
                    continue
                family = font.get('typeface')
                if not all(char.isspace() or (family, char) in RUN_SYMBOLS for char in node.text):
                    continue
                original = node.text
                node.text = ''.join(RUN_SYMBOLS.get((family, char), char) for char in original)
                if node.text == original:
                    continue
                found.append({'kind': 'symbol-run', 'font': dict(font.attrib), 'char': original, 'unicode': node.text,
                              'mapping_reference': 'https://www.alanwood.net/demos/wingdings.html'})
                font.set('typeface', FONT_NAME)
                font.attrib.pop('charset', None)
                for tag in ['latin', 'ea', 'cs']:
                    selected = properties.find('{' + NS + '}' + tag)
                    if selected is None:
                        selected = etree.SubElement(properties, '{' + NS + '}' + tag)
                    selected.set('typeface', FONT_NAME)
                    selected.attrib.pop('charset', None)
            if found:
                replacements[info.filename] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
                changes.append({'part': info.filename, 'original_sha256': hashlib.sha256(raw).hexdigest(),
                                'original_xml': raw.decode('utf-8'), 'bullets': found})
        if not changes:
            return source, None
        target = work/'unicode-symbols.pptx'
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            for info in archive.infolist():
                output.writestr(info, replacements.get(info.filename, archive.read(info)))
    font = installed_font()
    report = {'profile': PROFILE, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'prepared_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
              'mapping_reference': MAPPING_REFERENCE, 'font': {'file': font.name, 'sha256': FONT_SHA256,
              'bytes': FONT_BYTES, 'revision': FONT_REVISION}, 'changes': changes}
    (work/'symbol-conversion.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return target, report
