"""Produce a local HTML candidate for one verified SingleFile archive, offline.

Use the original (unmodified) source digest in any subsequent managed review.
This opt-in conversion is not part of the automatic Web-profile candidate cache.
"""
import argparse
import base64
import io
from pathlib import Path
import re

from . import reader_web_managed_convert as managed
from .reader_web_managed_review import MAX_DOC

SOURCE_SHA256 = 'ccc48863d9f5d6449489bbd2ac3a6a7644cf881df05b35dffb22337d50ea64d5'
# CSS variable, dimensions, decoded size, placement count, PNG SHA-256.
AVATARS = (
    (2, 45, 534, 8, '48b13dbd120ca8e4c45b4e622c338d48e1c6c139181969bafb4ba235f2527c56'),
    (6, 20, 300, 2, '1c65f42e8819f619d929280d170c0c4c12f8ca6f77e9edbe23fc9971d56877e8'),
)
PNG_SRC = b'data:image/png;base64,'


def restore(raw):
    if len(raw) > managed.MAX_ITEM_SOURCE or managed.digest(raw) != SOURCE_SHA256:
        raise managed.Hold('avatar_source_mismatch')

    # Match only the literal SingleFile :root prefix in the reviewed bytes.
    # No CSS evaluation, escaping, URL lookups, or fallback to another rule.
    declarations = []
    for key, _, size, _, _ in AVATARS:
        name = b'--sf-img-%d:' % key
        if raw.count(name) != 1:
            raise managed.Hold('avatar_mapping_mismatch')
        encoded_size = 4 * ((size + 2) // 3)
        declarations.append(name + rb'url\("' + PNG_SRC + rb'([A-Za-z0-9+/=]{%d})"\)' % encoded_size)
    roots = list(re.finditer(rb'<style>:root\{' + b';'.join(declarations) + rb'\}', raw))
    if len(roots) != 1:
        raise managed.Hold('avatar_mapping_mismatch')

    for (key, side, size, placements, sha), payload in zip(AVATARS, roots[0].groups()):
        try:
            png = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise managed.Hold('avatar_png_mismatch') from exc
        if len(png) != size or managed.digest(png) != sha or base64.b64encode(png) != payload:
            raise managed.Hold('avatar_png_mismatch')
        try:
            with managed.producer.Image.open(io.BytesIO(png)) as image:
                if image.format != 'PNG' or image.size != (side, side) or image.n_frames != 1:
                    raise managed.Hold('avatar_png_mismatch')
                image.verify()
        except (OSError, SyntaxError, managed.producer.Image.DecompressionBombError) as exc:
            raise managed.Hold('avatar_png_mismatch') from exc

        # Match the whole reviewed img tag, including the exact inert SVG
        # placeholder, avatar class, dimensions and inline variable reference.
        # Replacing just src leaves all other input bytes alone.
        placeholder = (b'''src='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" '''
                       b'''width="%d" height="%d"><rect fill-opacity="0"/></svg>' ''') % (side, side)
        start = b'<img alt width=%d height=%d ' % (side, side)
        end = ('title=捷尔任斯基 class=avatar style="background-blend-mode:normal!important;'
               'background-clip:content-box!important;background-position:50% 50%!important;'
               'background-color:rgba(0,0,0,0)!important;'
               f'background-image:var(--sf-img-{key})!important;background-size:100% 100%!important;'
               'background-origin:content-box!important;background-repeat:no-repeat!important">').encode('utf-8')
        tag = start + placeholder + end
        if raw.count(tag) != placements:
            raise managed.Hold('avatar_placeholder_mismatch')
        raw = raw.replace(tag, start + b'src="' + PNG_SRC + payload + b'" ' + end)
    return raw


def convert(raw, *, omissions=None):
    def no_fetch(_):
        raise managed.Hold('avatar_resource_fetch_forbidden')

    # Standard image byte/pixel/MIME checks, budgets, sanitizer and parity
    # still apply. No raw CSS or SVG makes it into the image output.
    return managed.convert(restore(raw), 'html', 'https://huggingface.co/local/source.html',
                           managed.Downloads(managed.MAX_SOURCE_BYTES, no_fetch), MAX_DOC,
                           omissions=omissions)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(argv)
    try:
        with args.source.open('rb') as stream:
            raw = stream.read(managed.MAX_ITEM_SOURCE + 1)
        document = convert(raw)
        with args.output.open('xb') as stream:
            stream.write(document)
    except (OSError, managed.Hold) as exc:
        parser.exit(2, f'cannot produce exact avatar candidate: {exc}\n')


if __name__ == '__main__':
    main()
