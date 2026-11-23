import base64
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import reader_web_managed_avatar as avatar, reader_web_managed_convert as managed


def fixture(*, image_format='PNG', image_side=2, corrupt=False):
    out = io.BytesIO()
    managed.producer.Image.new('RGB', (image_side, image_side)).save(out, format=image_format)
    png = out.getvalue()
    if corrupt:
        png = png[:-10]
    src = b'data:image/png;base64,' + base64.b64encode(png)
    html = (b'<!doctype html><style>:root{--sf-img-2:url("' + src + b'")}</style><p>body</p>'
            b'''<img alt width=2 height=2 src='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" '''
            b'''width="2" height="2"><rect fill-opacity="0"/></svg>' '''
            + ('title=捷尔任斯基 class=avatar style="background-blend-mode:normal!important;'
               'background-clip:content-box!important;background-position:50% 50%!important;'
               'background-color:rgba(0,0,0,0)!important;background-image:var(--sf-img-2)!important;'
               'background-size:100% 100%!important;background-origin:content-box!important;'
               'background-repeat:no-repeat!important">').encode())
    return html, src, ((2, 2, len(png), 1, managed.digest(png)),)


class ExactAvatarTests(unittest.TestCase):
    def test_production_source_gate_rejects_near_match_and_does_not_create_file(self):
        raw, _, _ = fixture()
        with self.assertRaisesRegex(managed.Hold, 'avatar_source_mismatch'):
            avatar.convert(raw)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.html'
            output = source.with_name('output.html')
            source.write_bytes(raw)
            result = subprocess.run([sys.executable, '-B', '-m', 'scripts.reader_web_managed_avatar',
                                     str(source), str(output)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b'avatar_source_mismatch', result.stderr)
            self.assertFalse(output.exists())

    def test_only_opt_in_restores_and_uses_normal_image_pipeline(self):
        raw, src, mapping = fixture()
        placeholder = raw[raw.index(b"src='data:image/svg+xml"):raw.index(b"' title=")+1]
        notes = {}
        standard = managed.convert(raw, 'html', 'https://huggingface.co/', None, avatar.MAX_DOC, omissions=notes)
        self.assertNotIn(b'<img', standard)
        self.assertEqual(notes, {'unsupported_images': 1})
        notes = {}
        with patch.object(avatar, 'AVATARS', mapping), patch.object(avatar, 'SOURCE_SHA256', managed.digest(raw)):
            self.assertEqual(avatar.restore(raw), raw.replace(placeholder, b'src="' + src + b'"'))
            doc = avatar.convert(raw, omissions=notes)
            with patch.object(managed, 'MAX_RESOURCE', 1), self.assertRaisesRegex(managed.Hold, 'resource_byte_limit'):
                avatar.convert(raw)
            with patch.object(avatar, 'MAX_DOC', 1), self.assertRaisesRegex(managed.Hold, 'output_budget'):
                avatar.convert(raw)
        self.assertEqual(notes, {})
        img, = managed.parse_html(doc.decode()).iter('img')
        self.assertEqual(img.get('src').encode(), src)
        for value in (b'<svg', b'--sf-img', b'data:image/svg+xml', b'background-image:var('):
            self.assertNotIn(value, doc)

    def test_mapping_and_placeholder_drift_fail_closed_even_with_source_pin(self):
        raw, src, mapping = fixture()
        for old, new, reason in (
                (src, b'https://example.invalid/avatar.png', 'mapping'),
                (b'--sf-img-2:url', b'--sf-img-3:url', 'mapping'),
                (b'</style>', b'</style><style>:root{--sf-img-2:none}</style>', 'mapping'),
                (b'class=avatar', b'class=not-avatar', 'placeholder'),
                (b'var(--sf-img-2)', b'var(--sf-img-6)', 'placeholder'),
                (b'fill-opacity="0"', b'fill-opacity="1"', 'placeholder'),
                (b'height="2"', b'height="3"', 'placeholder'),
                (b'/>', b'/><image href="https://example.invalid/a"/>', 'placeholder')):
            changed = raw.replace(old, new)
            with self.subTest(old=old), patch.object(avatar, 'AVATARS', mapping), \
                    patch.object(avatar, 'SOURCE_SHA256', managed.digest(changed)), \
                    self.assertRaisesRegex(managed.Hold, f'avatar_{reason}_mismatch'):
                avatar.convert(changed)

    def test_png_digest_format_dimensions_and_integrity(self):
        raw, _, mapping = fixture()
        bad_pin = (mapping[0][:-1] + ('0'*64,),)
        for raw, mapping in ((raw, bad_pin), *(f[::2] for f in (
                fixture(image_format='GIF'), fixture(image_side=3), fixture(corrupt=True)))):
            with patch.object(avatar, 'AVATARS', mapping), \
                    patch.object(avatar, 'SOURCE_SHA256', managed.digest(raw)), \
                    self.assertRaisesRegex(managed.Hold, 'avatar_png_mismatch'):
                avatar.convert(raw)

    def test_even_the_exact_path_disallows_dependency_requests(self):
        raw, _, mapping = fixture()
        raw += b'<img src="local.png">'
        with patch.object(avatar, 'AVATARS', mapping), \
                patch.object(avatar, 'SOURCE_SHA256', managed.digest(raw)), \
                patch.object(managed, 'anonymous_fetch') as fetch, \
                self.assertRaisesRegex(managed.Hold, 'avatar_resource_fetch_forbidden'):
            avatar.convert(raw)
        fetch.assert_not_called()
