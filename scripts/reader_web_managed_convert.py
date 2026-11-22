#!/usr/bin/env python3
"""Build local web candidates from a v1 request (never publish or access a bucket).

Request: {version: 1, max_rows, max_source_bytes, max_output_bytes, items}.
Source budget covers cumulative downloaded body bytes, including resources and
failed rows. Output budget covers reported source/document/manifest members;
report metadata is separately bounded by the row cap. Use a dedicated output
directory. Reruns revalidate the pinned inputs and reuse only identical complete
entries; holds can be retried. No live Reader or release checks are performed.
"""
from __future__ import annotations

import argparse
import base64
import email.policy
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
import urllib.request
from collections import Counter
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit

from . import convert_reader_assets as producer

EXTENSIONS = frozenset({'htm', 'html', 'mht', 'mhtml'})
MAX_ROWS = 40
MAX_REQUEST_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_ITEM_SOURCE = 8 * 1024 * 1024
MAX_DOCUMENT = 24 * 1024 * 1024
MAX_RESOURCE = 2 * 1024 * 1024
MAX_RESOURCES = 32
IMAGE_TYPES = {'.gif': 'image/gif', '.jpeg': 'image/jpeg', '.jpg': 'image/jpeg',
               '.png': 'image/png', '.webp': 'image/webp'}
HF = 'huggingface.co'
CDN = frozenset({'us.aws.cdn.hf.co', 'eu.aws.cdn.hf.co', 'cdn-lfs.hf.co',
                 'cdn-lfs-us-1.hf.co', 'cdn-lfs-eu-1.hf.co', 'cas-bridge.xethub.hf.co',
                 'transfer.xethub.hf.co'})
FIELDS = {'repo', 'path', 'extension', 'source_revision', 'source_sha256'}
LIMITS = {'max_rows': MAX_ROWS, 'max_source_bytes': MAX_SOURCE_BYTES, 'max_output_bytes': MAX_OUTPUT_BYTES}


class Hold(ValueError):
    """Stable, non-secret per-item hold reason."""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('ascii')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def relative_path(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and not re.search(r'[\\%\x00-\x1f\x7f\ud800-\udfff]', value)
            and all(part not in {'', '.', '..'} for part in value.split('/')))


def validate_request(request):
    if (not isinstance(request, dict) or request.keys() != {'version', 'items', *LIMITS}
            or type(request['version']) is not int or request['version'] != 1):
        raise ValueError('expected a version 1 web conversion request')
    for key, cap in LIMITS.items():
        if type(request[key]) is not int or not 0 < request[key] <= cap:
            raise ValueError(f'{key} must be between 1 and {cap}')
    if not isinstance(request['items'], list) or not 0 < len(request['items']) <= request['max_rows']:
        raise ValueError('items outside row budget')
    seen = set()
    for item in request['items']:
        if (not isinstance(item, dict) or item.keys() != FIELDS
                or not all(isinstance(v, str) for v in item.values())):
            raise ValueError('invalid item fields')
        repo, path, extension = (item[k] for k in ('repo', 'path', 'extension'))
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', repo)
                or not relative_path(path) or extension not in EXTENSIONS
                or Path(path).suffix.lower() != '.' + extension
                or not re.fullmatch(r'[0-9a-f]{40}', item['source_revision'])
                or not re.fullmatch(r'[0-9a-f]{64}', item['source_sha256'])):
            raise ValueError('invalid immutable web source identity')
        key = (repo.casefold(), path)
        if key in seen:
            raise ValueError('duplicate web source row')
        seen.add(key)


def check_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {HF, *CDN}
            or parsed.netloc not in {parsed.hostname, f'{parsed.hostname}:443'} or parsed.fragment):
        raise Hold('download_destination')
    return parsed


class Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = check_url(req.full_url), check_url(newurl)
        if after.hostname == HF:
            # Git-backed files normally go through the Hub's resolve cache.
            # This exception is only for the identical repo, commit and path.
            match = re.fullmatch(r'/datasets/([^/]+/[^/]+)/resolve/([0-9a-f]{40})/(.+)', before.path)
            cache_path = f'/api/resolve-cache/datasets/{match[1]}/{match[2]}/{match[3]}' if match else None
            same_file = after.path == before.path and after.query == before.query
            same_cache = (cache_path is not None and unquote(after.path) == unquote(cache_path)
                          and all(k in {'download', 'etag'} for k, _ in parse_qsl(after.query, max_num_fields=4)))
            if before.hostname != HF or not (same_file or same_cache):
                raise Hold('download_destination')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Downloads:
    def __init__(self, remaining, fetch):
        self.remaining = remaining
        self.fetch = fetch

    def get(self, url, limit):
        if self.remaining <= 0:
            raise Hold('source_budget')
        stream = io.BytesIO()
        limit = min(limit, self.remaining)
        deadline = time.monotonic() + 60
        try:
            with self.fetch(url) as response:
                headers = response.headers
                length = headers.get('Content-Length')
                if (response.status != 200 or headers.get('Content-Range') is not None
                        or headers.get('Content-Encoding', 'identity').lower() != 'identity'
                        or (length is not None and not re.fullmatch(r'[0-9]{1,12}', length))):
                    raise Hold('download_response')
                if length is not None and int(length) > limit:
                    raise Hold('source_budget' if limit == self.remaining else 'item_byte_limit')
                while True:
                    if time.monotonic() > deadline:
                        raise Hold('download_timeout')
                    # One overflow probe permits exact-limit files, without silently truncating.
                    chunk = response.read(min(64 * 1024, limit - stream.tell() + 1))
                    if not chunk:
                        break
                    self.remaining -= len(chunk)
                    if len(chunk) + stream.tell() > limit or self.remaining < 0:
                        raise Hold('source_budget' if self.remaining <= 0 else 'item_byte_limit')
                    stream.write(chunk)
                if not stream.tell() or (length is not None and stream.tell() != int(length)):
                    raise Hold('download_response')
        except Hold:
            raise
        except Exception as exc:
            raise Hold('download_failed') from exc
        return stream.getvalue()


def anonymous_fetch(url):
    if check_url(url).hostname != HF:
        raise Hold('download_destination')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
    return opener.open(urllib.request.Request(url, headers={
        'User-Agent': 'Reader-Web-Managed/1', 'Accept-Encoding': 'identity',
    }), timeout=15)


def parse_html(text):
    return producer.html5lib_shim.HTMLParser(namespaceHTMLElements=False).parse(text)


def mhtml_body(raw):
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    parts = list(message.walk())
    if len(parts) > 256:
        raise Hold('mhtml_parts_limit')
    start = message.get_param('start') if message.get_content_type() == 'multipart/related' else None
    if start:
        wanted = start.strip().strip('<>')
        roots = [p for p in message.iter_parts() if wanted and
                 str(p.get('Content-ID') or '').strip().strip('<>') == wanted]
    else:
        roots = [p for p in parts if p.get_content_type() == 'text/html'][:1]
    if len(roots) != 1 or roots[0].get_content_type() != 'text/html':
        raise Hold('mhtml_root')
    body = roots[0]
    base = str(body.get('Content-Location') or '').replace('\\', '/')
    resources = {}
    for part in parts:
        mime = part.get_content_type()
        if mime not in {*IMAGE_TYPES.values(), 'text/css'}:
            continue
        location = str(part.get('Content-Location') or '').replace('\\', '/')
        cid = str(part.get('Content-ID') or '').strip().strip('<>')
        keys = ({location, urljoin(base, location)} if location else set()) | ({'cid:' + cid} if cid else set())
        for key in keys:
            if key in resources and resources[key] is not part:
                raise Hold('mhtml_ambiguous_resource')
            resources[key] = part

    def lookup(value, allowed):
        part = resources.get(value) or resources.get(urljoin(base, value.replace('\\', '/')))
        if part is None or part.get_content_type() not in allowed:
            return None
        return part.get_content_type(), part.get_payload(decode=True) or b''

    return producer.decode_html_bytes(body.get_payload(decode=True) or b'', body.get_content_charset()), lookup


def tree_lookup(source_url, downloads):
    base = source_url.rsplit('/', 1)[0] + '/'
    prefix = urlsplit(base).path

    def lookup(value, allowed):
        parsed = urlsplit(urljoin(base, value))
        if (parsed.scheme != 'https' or parsed.netloc != HF or parsed.query or parsed.fragment
                or not parsed.path.startswith(prefix)):
            return None
        relative = unquote(parsed.path[len(prefix):], errors='strict')
        if not relative_path(relative):
            return None
        suffix = Path(relative).suffix.lower()
        mime = 'text/css' if suffix == '.css' else IMAGE_TYPES.get(suffix)
        if mime not in allowed:
            return None
        return mime, downloads.get(base + quote(relative, safe='/'), MAX_RESOURCE)

    return lookup


def parity(root):
    body = root.find('body')
    title = root.find('head/title')
    table_tags = {'table', 'caption', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'col', 'colgroup'}
    return {
        'text': (' '.join(''.join(body.itertext()).split()), '' if title is None else ''.join(title.itertext())),
        'anchors': Counter((k, node.get(k)) for node in root.iter() for k in ('id', 'name', 'href')
                           if node.get(k) and (k == 'id' or (node.tag == 'a' and
                               (k == 'name' or node.get(k).strip().startswith('#'))))),
        'tables': [(node.tag, tuple(node.get(k) for k in ('colspan', 'rowspan', 'scope', 'headers', 'span')),
                    sum(1 for child in node if child.tag in table_tags))
                   for node in body.iter() if node.tag in table_tags],
        'pre': [''.join(node.itertext()) for node in body.iter('pre')],
        'images': [(node.get('src'), node.get('alt')) for node in body.iter('img')],
    }


def detach(parent, node):
    children = list(parent)
    index = children.index(node)
    if node.tail:
        if index:
            previous = children[index - 1]
            previous.tail = (previous.tail or '') + node.tail
        else:
            parent.text = (parent.text or '') + node.tail
    parent.remove(node)


def convert(raw, extension, url, downloads, limit):
    if extension in {'mht', 'mhtml'}:
        text, lookup = mhtml_body(raw)
    else:
        text, lookup = producer.decode_html_bytes(raw), tree_lookup(url, downloads)
    if '\ufffd' in text or '\x00' in text:
        raise Hold('source_decoding')
    tree = parse_html(producer.drop_html_scripts(text, web_typography=True))
    cache = {}
    expansion = 0

    def resource(value, allowed):
        value = value.strip()
        if not value or re.search(r'[\x00-\x1f\x7f]', value):
            return None
        key = (value, allowed)
        if key not in cache:
            if len(cache) >= MAX_RESOURCES:
                raise Hold('resource_count')
            cache[key] = lookup(value, allowed)
        result = cache[key]
        if result is not None and not 0 < len(result[1]) <= MAX_RESOURCE:
            raise Hold('resource_byte_limit')
        return result

    for node in tree.iter():
        if node.tag in {'iframe', 'frame', 'frameset', 'object', 'embed', 'video', 'audio', 'canvas',
                        '{http://www.w3.org/2000/svg}svg'} or (node.tag in {'source', 'img'} and node.get('srcset')):
            raise Hold('unsupported_embedded_content')
        if node.tag != 'img':
            continue
        value = node.get('src', '').strip()
        match = re.fullmatch(r'data:(image/(?:gif|jpeg|png|webp));base64,([A-Za-z0-9+/=]+)', value, re.I)
        if match:
            if len(match[2]) > (MAX_RESOURCE + 2) // 3 * 4:
                raise Hold('resource_byte_limit')
            result = match[1].lower(), base64.b64decode(match[2], validate=True)
        else:
            types = frozenset(IMAGE_TYPES.values())
            result = resource(value, types) or resource(node.get('data-src', ''), types)
        if not result:
            raise Hold('missing_image')
        mime, image = result
        if not 0 < len(image) <= MAX_RESOURCE:
            raise Hold('resource_byte_limit')
        try:
            with producer.Image.open(io.BytesIO(image)) as raster:
                if raster.width * raster.height > 24_000_000 or producer.Image.MIME.get(raster.format) != mime:
                    raise Hold('invalid_image')
                raster.verify()
        except Exception as exc:
            raise Hold('invalid_image') from exc
        expansion += (len(image) + 2) // 3 * 4
        if expansion > limit:
            raise Hold('output_budget')
        node.set('src', f'data:{mime};base64,' + base64.b64encode(image).decode('ascii'))

    parents = {child: parent for parent in tree.iter() for child in parent if child.tag == 'link'}
    for node, parent in parents.items():
        if ('stylesheet' not in node.get('rel', '').lower().split()
                or node.get('media', '').lower() not in {'', 'all', 'screen'}):
            detach(parent, node)
            continue
        css = resource(node.get('href', ''), frozenset({'text/css'}))
        if css is None:
            raise Hold('missing_stylesheet')
        expansion += len(css[1])
        if expansion > limit:
            raise Hold('output_budget')
        node.tag, node.text = 'style', producer.sanitize_css(producer.decode_html_bytes(css[1]))
        if '<' in node.text:
            raise Hold('invalid_stylesheet')
        node.attrib.clear()

    expected = parity(tree)
    if not expected['text'][0] and not expected['images']:
        raise Hold('missing_content')
    producer.preserve_pre_newlines(tree)
    renderer = producer.html5lib_shim.HTMLSerializer(
        quote_attr_values='always', escape_lt_in_attrs=True, omit_optional_tags=False)
    output = producer.sanitize_web_html(renderer.render(producer.html5lib_shim.getTreeWalker('etree')(tree)))
    encoded = output.encode('utf-8')
    if len(encoded) > limit:
        raise Hold('output_budget')
    rendered = parse_html(output)
    for node in rendered.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag in {'script', 'base', 'link', 'iframe', 'object', 'embed'}:
            raise Hold('unsafe_document')
        for attr, value in node.attrib.items():
            if (attr.startswith('on') or attr in {'srcset', 'http-equiv', 'action', 'formaction', 'srcdoc'}
                    or (attr == 'src' and not re.fullmatch(r'data:image/(?:gif|jpeg|png|webp);base64,[A-Za-z0-9+/=]+', value))
                    or (attr == 'href' and not value.strip().startswith('#'))):
                raise Hold('unsafe_document')
    actual = parity(rendered)
    for kind in expected:
        if actual[kind] != expected[kind]:
            raise Hold('parity_' + kind)
    return encoded


def ensure_directory(path):
    path.mkdir(exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('output directory is not a real directory')


def verify_entry(entry, members):
    if entry.is_symlink() or not entry.is_dir() or {p.name for p in entry.iterdir()} != members.keys():
        raise Hold('local_entry_conflict')
    for name, raw in members.items():
        path = entry / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(raw) or digest(path.read_bytes()) != digest(raw):
            raise Hold('local_entry_conflict')


def store_entry(output, extension, bundle_id, members):
    parent = output / 'entries'
    ensure_directory(parent)
    parent /= extension
    ensure_directory(parent)
    entry = parent / bundle_id
    if not entry.exists() and not entry.is_symlink():
        with tempfile.TemporaryDirectory(prefix='.entry-', dir=parent) as temporary:
            stage = Path(temporary) / 'complete'
            stage.mkdir()
            for name, raw in members.items():
                (stage / name).write_bytes(raw)
            verify_entry(stage, members)
            try:
                stage.rename(entry)
            except OSError:
                if not entry.exists():
                    raise
    verify_entry(entry, members)
    return entry.relative_to(output).as_posix()


def run(request, output: Path, *, fetch=anonymous_fetch, request_sha256=None):
    validate_request(request)
    output.mkdir(parents=True, exist_ok=True)
    ensure_directory(output)
    downloads = Downloads(request['max_source_bytes'], fetch)
    remaining = request['max_output_bytes']
    rows = []
    for item in request['items']:
        row = dict(item)
        try:
            if remaining <= 0:
                raise Hold('output_budget')
            extension = item['extension']
            url = f"https://{HF}/datasets/{item['repo']}/resolve/{item['source_revision']}/{quote(item['path'])}"
            raw = downloads.get(url, MAX_ITEM_SOURCE)
            if digest(raw) != item['source_sha256']:
                raise Hold('source_sha256_mismatch')
            if len(raw) >= remaining:
                raise Hold('output_budget')
            document = convert(raw, extension, url, downloads, min(MAX_DOCUMENT, remaining - len(raw)))
            doc_sha = digest(document)
            manifest = encode({'kind': 'web-document-stream', 'version': 1, 'path': 'document.html',
                               'source_extension': extension, 'source_sha256': item['source_sha256'],
                               'bytes': len(document), 'sha256': doc_sha})
            bundle_id = digest(f"{item['source_sha256']}\0{doc_sha}".encode('ascii'))
            members = {f'source.{extension}': raw, 'document.html': document, 'document-manifest.json': manifest}
            size = sum(len(data) for data in members.values())
            if size > remaining:
                raise Hold('output_budget')
            entry = store_entry(output, extension, bundle_id, members)
            remaining -= size
            row.update(status='converted', bundle_sha256=bundle_id, files={name: {
                'path': f'{entry}/{name}', 'bytes': len(data), 'sha256': digest(data),
            } for name, data in members.items()})
        except Hold as exc:
            row.update(status='hold', reason=str(exc))
        except Exception:
            row.update(status='hold', reason='conversion_or_local_io_failed')
        rows.append(row)
    report = {'kind': 'reader-web-managed-conversion', 'version': 1,
              'request_sha256': request_sha256 or digest(encode(request)),
              'items': rows, 'output_bytes': request['max_output_bytes'] - remaining,
              'dual_reader_verified': False, 'publication_ready': False, 'remote_writes': 0}
    with tempfile.TemporaryDirectory(prefix='.report-', dir=output) as temporary:
        path = Path(temporary) / 'report.json'
        path.write_bytes(encode(report))
        os.replace(path, output / 'report.json')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with args.request.open('rb') as source:
            raw = source.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('request exceeds byte limit')
        run(json.loads(raw, object_pairs_hook=unique_object), args.output, request_sha256=digest(raw))
    except (OSError, ValueError, RecursionError) as exc:
        parser.exit(2, f'invalid web conversion request or output: {exc}\n')
    # Holds are reported per item and do not prevent collecting the artifact.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
