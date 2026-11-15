"""Source CHM navigation, with Windows path resolution and retained group nodes.

This module never executes CHM code. Missing/ambiguous targets remain explicit.
"""
from html.parser import HTMLParser
import posixpath
import re
import struct
import urllib.parse
import zipfile
from collections import Counter
from lxml import etree, html as HTML

try:
    from . import convert_reader_assets as converter
except ImportError:
    import convert_reader_assets as converter


def label(text):
    return re.sub(r'\s+', ' ', re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text or '')).strip()


class SitemapParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.levels = [self.nodes]
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'ul':
            siblings = self.levels[-1]
            self.levels.append(siblings[-1]['children'] if siblings else siblings)
        elif tag == 'object':
            self.current = {} if attrs.get('type', 'text/sitemap').lower() == 'text/sitemap' else None
        elif tag == 'param' and self.current is not None:
            name = attrs.get('name', '').lower()
            if name in {'name', 'local'}:
                self.current[name] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'ul' and len(self.levels) > 1:
            self.levels.pop()
        elif tag == 'object' and self.current is not None:
            if self.current.get('name') or self.current.get('local'):
                self.levels[-1].append({'label': label(self.current.get('name')),
                                       'href': self.current.get('local', '').strip(), 'children': []})
            self.current = None


def flatten(nodes, depth=0):
    for node in nodes:
        yield node, depth
        yield from flatten(node['children'], depth + 1)


def decode(path):
    data = path.read_bytes()
    if data.startswith(b'\xef\xbb\xbf'):
        try:
            return data.decode('utf-8-sig')
        except UnicodeDecodeError:
            pass  # Some Windows CHMs prepend a UTF-8 BOM to GBK content.
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    return converter.decode_html_source(path)


class SourcePaths:
    def __init__(self, names):
        self.names = set(names)
        self.folded = {}
        for name in self.names:
            self.folded.setdefault(name.casefold(), []).append(name)

    def resolve(self, base, href):
        # URL components must be split before percent decoding: %23 is a filename.
        parts = urllib.parse.urlsplit(href.replace('\\', '/'))
        if parts.scheme or parts.netloc:
            return None, '', 'external'
        raw = urllib.parse.unquote(parts.path)
        name = posixpath.normpath(posixpath.join(posixpath.dirname(base), raw)) if raw else base
        if raw.startswith('/'):
            name = posixpath.normpath(raw.lstrip('/'))
        if name.startswith('../') or name == '..':
            return None, parts.fragment, 'outside'
        if name in self.names:
            return name, parts.fragment, 'exact'
        matches = self.folded.get(name.casefold(), [])
        if len(matches) == 1:
            return matches[0], parts.fragment, 'casefold'
        return None, parts.fragment, 'ambiguous' if matches else 'missing'


def navigation(root):
    paths = SourcePaths(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
    hhcs = sorted(n for n in paths.names if n.lower().endswith('.hhc'))
    system = root / '#SYSTEM'
    metadata = {}
    if system.exists():
        data, offset = system.read_bytes(), 4
        while offset + 4 <= len(data):
            code, size = struct.unpack_from('<HH', data, offset)
            offset += 4
            if offset + size > len(data):
                break
            metadata[code] = data[offset:offset + size].rstrip(b'\0')
            offset += size
    selected = None
    for code in (0, 2):
        value = metadata.get(code, b'')
        for encoding in ('utf-8', 'gb18030', 'cp1252'):
            try:
                candidate, _, _ = paths.resolve('', value.decode(encoding))
            except (UnicodeDecodeError, ValueError):
                continue
            if candidate in hhcs:
                selected = candidate
                break
        if selected:
            break
    parsed = {}
    for name in hhcs:
        parser = SitemapParser()
        parser.feed(decode(root / name))
        parsed[name] = parser.nodes
    if selected is None and hhcs:
        nonempty = [n for n in hhcs if parsed[n]]
        if len(nonempty) == 1:
            selected = nonempty[0]
        elif len(nonempty) > 1:
            # Identical copies are unambiguous; different source trees need review.
            if all(parsed[n] == parsed[nonempty[0]] for n in nonempty):
                selected = nonempty[0]
            else:
                raise ValueError('multiple source HHC trees without a designated contents file: ' + repr(nonempty))
    nodes = parsed.get(selected, [])
    if not nodes and not hhcs:
        try:
            from . import chm_static_navigation
        except ImportError:
            import chm_static_navigation
        return chm_static_navigation.navigation(root, paths, decode, label), paths
    for node, depth in flatten(nodes):
        if node['href']:
            name, fragment, status = paths.resolve(selected, node['href'])
            node.update(source=name, fragment=fragment, resolution=status)
        else:
            node.update(source=None, fragment='', resolution='group')
    return {'hhc': selected, 'hhc_files': hhcs, 'nodes': nodes}, paths


def repair_epub(path, root, mapping):
    """Repair navigation without regenerating chapter content or guessing targets.

    `mapping` is the reviewed source-page to output-member correspondence. Every
    original HHC target must be represented or explicitly absent from the source.
    """
    structure, source_paths = navigation(root)
    if not structure['nodes']:
        return {'changed': False, 'no_hhc': True}
    with zipfile.ZipFile(path) as archive:
        infos = {i.filename: i for i in archive.infolist()}
        content = {n: archive.read(n) for n in infos}
    original_content = dict(content)
    package_path = etree.fromstring(content['META-INF/container.xml']).find('.//{*}rootfile').get('full-path')
    package = etree.fromstring(content[package_path])
    manifest = package.find('{*}manifest')
    spine = package.find('{*}spine')
    ns = '{http://www.idpf.org/2007/opf}'
    def resolve(href):
        return posixpath.normpath(posixpath.join(posixpath.dirname(package_path), urllib.parse.unquote(href)))
    items = {i.get('id'): resolve(i.get('href')) for i in manifest}
    spine_names = [items[i.get('idref')] for i in spine]
    document_names = {resolve(i.get('href')) for i in manifest if i.get('media-type') in {'application/xhtml+xml', 'text/html'}}
    documents = {n: etree.fromstring(content[n]) for n in document_names}
    ids = {n: set(d.xpath('//@id | //@name')) for n, d in documents.items()}
    issues, resolved, order = [], {}, []
    def destination(node):
        source = node['source']
        if not source:
            if node['href']:
                issues.append({'kind': 'source_target_' + node['resolution'], 'href': node['href'], 'label': node['label']})
            return ''
        mapped = mapping.get(source, [])
        candidates = [n.split('#', 1)[0] for n in mapped]
        if not candidates or any(n not in documents for n in candidates):
            raise ValueError('unmapped original TOC page: ' + source)
        for n in candidates:
            if n not in order:
                order.append(n)
        fragment = urllib.parse.unquote(node['fragment'])
        if fragment:
            found = []
            for target in mapped:
                name, _, section_id = target.partition('#')
                anchor = (section_id + '--' + fragment
                          if re.fullmatch('chm-page-[0-9]+', section_id) else fragment)
                if anchor in ids[name]:
                    found.append(name + '#' + urllib.parse.quote(anchor, safe='!$&\'()*+,;=:@/?'))
            if not found:
                # Retain exact source targets when a converter renamed anchors.
                original = HTML.document_fromstring(decode(root / source))
                anchors = original.xpath('//*[@id=$id or @name=$id]', id=fragment)
                if anchors:
                    raise ValueError('converted anchor missing: ' + source + '#' + fragment)
                issues.append({'kind': 'missing_source_anchor', 'href': node['href']})
                return mapped[0]
            return found[0]
        return mapped[0]
    for node, depth in flatten(structure['nodes']):
        resolved[id(node)] = destination(node)
    added_spine = []
    for name in order:
        if name not in spine_names:
            uid = next(uid for uid, member in items.items() if member == name)
            etree.SubElement(spine, ns+'itemref', idref=uid)
            added_spine.append(name)

    # Reuse existing navigation members, never overwrite a source chapter.
    ncx_item = next((i for i in manifest if i.get('media-type') == 'application/x-dtbncx+xml'), None)
    ncx_name = resolve(ncx_item.get('href')) if ncx_item is not None else resolve('reader-toc.ncx')
    nav_item = next((i for i in manifest if 'nav' in (i.get('properties') or '').split()), None)
    nav_name = resolve(nav_item.get('href')) if nav_item is not None else resolve('reader-nav.xhtml')
    ncx_ns = 'http://www.daisy.org/z3986/2005/ncx/'
    xhtml_ns = 'http://www.w3.org/1999/xhtml'
    epub_ns = 'http://www.idpf.org/2007/ops'
    ncx = etree.Element('{'+ncx_ns+'}ncx', nsmap={None:ncx_ns}, version='2005-1')
    title = ''.join(package.xpath('//*[local-name()="title"]/text()')) or '目录'
    head = etree.SubElement(ncx, '{'+ncx_ns+'}head')
    identifier = ''.join(package.xpath('//*[local-name()="identifier"]/text()'))
    etree.SubElement(head, '{'+ncx_ns+'}meta', name='dtb:uid', content=identifier)
    doc_title = etree.SubElement(ncx, '{'+ncx_ns+'}docTitle')
    etree.SubElement(doc_title, '{'+ncx_ns+'}text').text = title
    nav_map = etree.SubElement(ncx, '{'+ncx_ns+'}navMap')
    nav_doc = etree.Element('{'+xhtml_ns+'}html', nsmap={None:xhtml_ns, 'epub':epub_ns})
    nav_head = etree.SubElement(nav_doc, '{'+xhtml_ns+'}head')
    etree.SubElement(nav_head, '{'+xhtml_ns+'}title').text = '目录'
    body = etree.SubElement(nav_doc, '{'+xhtml_ns+'}body')
    nav_el = etree.SubElement(body, '{'+xhtml_ns+'}nav', {'{'+epub_ns+'}type':'toc'})
    ol = etree.SubElement(nav_el, '{'+xhtml_ns+'}ol')
    counter = 0
    def relative(target, base):
        name, sep, fragment = target.partition('#')
        return urllib.parse.quote(posixpath.relpath(name, posixpath.dirname(base) or '.'), safe='/') + (sep+fragment if sep else '') if target else ''
    def append(nodes, ncx_parent, html_parent):
        nonlocal counter
        for node in nodes:
            counter += 1
            point = etree.SubElement(ncx_parent, '{'+ncx_ns+'}navPoint', id=f'chm-toc-{counter}', playOrder=str(counter))
            nav_label = etree.SubElement(point, '{'+ncx_ns+'}navLabel')
            etree.SubElement(nav_label, '{'+ncx_ns+'}text').text = node['label'] or node['href']
            target = resolved[id(node)]
            etree.SubElement(point, '{'+ncx_ns+'}content', src=relative(target, ncx_name))
            li = etree.SubElement(html_parent, '{'+xhtml_ns+'}li')
            link = etree.SubElement(li, '{'+xhtml_ns+'}a' if target else '{'+xhtml_ns+'}span')
            if target:
                link.set('href', relative(target, nav_name))
            link.text = node['label'] or node['href']
            if node['children']:
                append(node['children'], point, etree.SubElement(li, '{'+xhtml_ns+'}ol'))
    append(structure['nodes'], nav_map, ol)
    def add_item(name, media_type, **attrs):
        uid = 'chm-' + str(len(manifest))
        while uid in items:
            uid += '-nav'
        item = etree.SubElement(manifest, ns+'item', id=uid, href=relative(name, package_path), **{'media-type':media_type}, **attrs)
        return item
    if ncx_item is None:
        ncx_item = add_item(ncx_name, 'application/x-dtbncx+xml')
    spine.set('toc', ncx_item.get('id'))
    if nav_item is None:
        nav_item = add_item(nav_name, 'application/xhtml+xml', properties='nav')
    content[ncx_name] = etree.tostring(ncx, encoding='utf-8', xml_declaration=True)
    content[nav_name] = etree.tostring(nav_doc, encoding='utf-8', xml_declaration=True)
    # Preserve every source page. Generated blank covers are not source content;
    # leave them in the package but exclude them from sequential reading.
    original_pages = {n.split('#', 1)[0] for values in mapping.values() for n in values}
    hidden = []
    source_cover = any(node['source'] and re.fullmatch(r'(?:封面|cover)(?:\.doc)?', node['label'], re.I)
        and any(documents[n.split('#',1)[0]].xpath('//*[local-name()="img" or local-name()="image"]')
                for n in mapping.get(node['source'],[]) if n.split('#',1)[0] in documents)
        for node, depth in flatten(structure['nodes']))
    for item in spine:
        name = items[item.get('idref')]
        if name in order:
            item.set('linear', 'yes')
        if name in original_pages or posixpath.basename(name) != 'titlepage.xhtml':
            continue
        doc = documents.get(name)
        if doc is not None:
            page_body = doc.find('{*}body')
            if page_body is not None and (source_cover or (not ''.join(page_body.itertext()).strip() and not page_body.xpath('.//*[local-name()="img" or local-name()="svg" or local-name()="image"]'))):
                item.set('linear', 'no')
                hidden.append(name)
    nonlinear = {member.split('#',1)[0] for source in structure.get('nonlinear_source_pages',[])
                 for member in mapping.get(source,[])} - set(order)
    for item in spine:
        if items[item.get('idref')] in nonlinear:
            item.set('linear','no')
    # HHC order governs listed chapters. Retain unlisted documents adjacent to
    # their previous predecessor, without promoting them into the source TOC.
    slots = [i for i, item in enumerate(spine) if items[item.get('idref')] in order]
    by_name = {items[item.get('idref')]: item for item in spine}
    ordered = [by_name[n] for n in order if n in by_name]
    if len(slots) != len(ordered):
        raise ValueError('duplicate spine targets require review')
    new_spine = list(spine)
    for slot, item in zip(slots, ordered):
        new_spine[slot] = item
    spine[:] = new_spine
    content[package_path] = etree.tostring(package, encoding='utf-8', xml_declaration=True)
    for name in document_names - {nav_name}:
        assert content[name] == original_content[name], name
    temporary = path.with_suffix('.navigation.epub')
    with zipfile.ZipFile(temporary, 'w') as archive:
        for name in sorted(content, key=lambda n: n != 'mimetype'):
            if name in infos:
                archive.writestr(infos[name], content[name])
            else:
                archive.writestr(name, content[name], compress_type=zipfile.ZIP_DEFLATED)
    temporary.replace(path)
    new_names = [items[i.get('idref')] for i in spine]
    return {'changed':True, 'toc_nodes':counter, 'source_issues':issues, 'spine_preserved':new_names == spine_names,
            'source_order':order, 'spine':new_names, 'excluded_generated_cover_pages':hidden,
            'added_spine_pages':added_spine, 'excluded_template_pages':sorted(nonlinear),
            'navigation_evidence':structure.get('navigation_evidence',{'kind':'hhc','file':structure['hhc']})}


def add_recovered_pages(path, recovered, checks, selected, mapping):
    """Import individually confirmed omitted pages, retaining existing chapters."""
    with zipfile.ZipFile(path) as archive:
        infos={i.filename:i for i in archive.infolist()}
        content={n:archive.read(n) for n in infos}
    opf=etree.fromstring(content['META-INF/container.xml']).find('.//{*}rootfile').get('full-path')
    package=etree.fromstring(content[opf]);manifest=package.find('{*}manifest');spine=package.find('{*}spine')
    ns='{'+etree.QName(manifest).namespace+'}'
    def relative(target, base):
        name,sep,fragment=target.partition('#')
        return urllib.parse.quote(posixpath.relpath(name,posixpath.dirname(base) or '.'),safe='/')+(sep+fragment if sep else '')
    recovered_map={'EPUB/'+c['chapter']:c['source'] for c in checks}
    new_names={n:'chm-restored/'+n for n,s in recovered_map.items() if s in selected}
    for n,s in recovered_map.items():
        if s in selected:mapping[s]=[new_names[n]]
    additions={}
    with zipfile.ZipFile(recovered) as archive:
        rpaths=SourcePaths(archive.namelist())
        for name,target in new_names.items():
            doc=etree.fromstring(archive.read(name))
            for node in doc.xpath('//*[@src or @href]'):
                for attr in ('src','href'):
                    raw=node.get(attr)
                    if not raw or raw.startswith('#'):continue
                    resolved,fragment,status=rpaths.resolve(name,raw)
                    if not resolved:continue
                    if resolved in recovered_map:
                        source=recovered_map[resolved]
                        if source not in mapping:raise ValueError('recovered page links to unmapped source: '+source)
                        dest=mapping[source][0]
                    else:
                        dest='chm-restored/'+resolved
                        additions[dest]=archive.read(resolved)
                    if fragment:dest=dest.split('#',1)[0]+'#'+fragment
                    node.set(attr,relative(dest,target))
            additions[target]=etree.tostring(doc,encoding='utf-8',xml_declaration=True)
    import mimetypes
    for n,data in additions.items():
        if n in content:raise ValueError('recovery member collision: '+n)
        uid='chm-restored-'+str(len(manifest))
        mime='application/xhtml+xml' if n in new_names.values() else mimetypes.guess_type(n)[0]
        etree.SubElement(manifest,ns+'item',id=uid,href=relative(n,opf),**{'media-type':mime or 'application/octet-stream'})
        if n in new_names.values():etree.SubElement(spine,ns+'itemref',idref=uid)
        content[n]=data
    content[opf]=etree.tostring(package,encoding='utf-8',xml_declaration=True)
    temporary=path.with_suffix('.recovered.epub')
    with zipfile.ZipFile(temporary,'w') as archive:
        for n in sorted(content,key=lambda n:n!='mimetype'):
            if n in infos:archive.writestr(infos[n],content[n])
            else:archive.writestr(n,content[n],compress_type=zipfile.ZIP_DEFLATED)
    temporary.replace(path)
    return sorted(selected)


def repair_conversion(source, target, work, source_sections=None):
    """Post-conversion gate: every original directory target needs a mapping."""
    try:
        from . import recover_chm, chm_resources
    except ImportError:
        import recover_chm, chm_resources
    root=work/'chm-navigation-source'
    root.mkdir(exist_ok=True)
    recover_chm.extract(source,root)
    structure, paths=navigation(root)
    with zipfile.ZipFile(target) as archive:
        container=etree.fromstring(archive.read('META-INF/container.xml'))
        opf=container.find('.//{*}rootfile').get('full-path')
        package=etree.fromstring(archive.read(opf));base=posixpath.dirname(opf)
        def absolute(href):
            return posixpath.normpath(posixpath.join(base,urllib.parse.unquote(href)))
        manifest=package.find('{*}manifest')
        documents={absolute(i.get('href')):etree.fromstring(archive.read(absolute(i.get('href'))))
                   for i in manifest if i.get('media-type') in {'application/xhtml+xml','text/html'}}
        output_paths=SourcePaths(documents)
        source_pages=sorted(n for n in paths.names if n.lower().endswith(('.htm','.html','.xhtml','.txt','.mht','.mhtml')))
        counts=Counter(posixpath.basename(n).casefold() for n in source_pages)
        by_base={};by_split={}
        for name in documents:
            by_base.setdefault(posixpath.basename(name).casefold(),[]).append(name)
            original=re.sub(r'_split_\d+(?=\.[^.]+$)','',posixpath.basename(name),flags=re.I)
            if original!=posixpath.basename(name):by_split.setdefault(original.casefold(),[]).append(name)
        items={i.get('id'):absolute(i.get('href')) for i in manifest}
        spine_names=list(dict.fromkeys(items[ref.get('idref')] for ref in package.find('{*}spine')))
        sections={}
        for name in spine_names:
            if name not in documents:
                continue
            for section in documents[name].xpath('//*[@id]'):
                section_id=section.get('id')
                if re.fullmatch('chm-page-[0-9]+',section_id):
                    sections.setdefault(section_id,[]).append(name+'#'+section_id)
        mapping={}
        owners=[]
        def assign(original, targets):
            # Every inference (path, split name, label or text) claims its actual
            # subtree. Equal or nested regions cannot prove two distinct pages,
            # even when their text matches (their images may differ).
            regions=[]
            for target in targets:
                region=mapped_region(target, documents)
                if region is None:
                    raise ValueError('unverifiable converted CHM target: '+original+' -> '+target)
                for owner, claimed in owners:
                    if owner!=original and (region is claimed or claimed in region.iterancestors()
                                            or region in claimed.iterancestors()):
                        raise ValueError('ambiguous converted CHM target reuse: '+owner+' / '+original)
                regions.append(region)
            mapping[original]=targets
            owners.extend((original,region) for region in regions)
        # Fallback ordinals belong to the producer's actual ordered inventory.
        # Never reconstruct them from HHC order or a differently sorted inventory.
        for name, section_id in (source_sections or {}).items():
            values=sections.get(section_id,[])
            if len(values)!=1:
                raise ValueError('missing or ambiguous converted CHM section: '+name+'#'+section_id)
            assign(name,values)
        for name in source_pages:
            if name in mapping:
                continue
            found,_,_=output_paths.resolve('',name)
            if found:assign(name,[found]);continue
            short=posixpath.basename(name).casefold()
            if counts[short]==1:
                candidates=by_base.get(short,[]) or sorted(by_split.get(short,[]))
                if candidates:assign(name,candidates)
        ncx=next((absolute(i.get('href')) for i in manifest if i.get('media-type')=='application/x-dtbncx+xml'),None)
        source_labels={};output_labels={}
        for node,depth in flatten(structure['nodes']):
            if node['source']:source_labels.setdefault(node['label'],set()).add(node['source'])
        if ncx:
            doc=etree.fromstring(archive.read(ncx))
            for point in doc.xpath('//*[local-name()="navPoint"]'):
                title=label(''.join(point.xpath('./*[local-name()="navLabel"]//text()')))
                href=''.join(point.xpath('./*[local-name()="content"]/@src'))
                if href:
                    parts=urllib.parse.urlsplit(href)
                    name=posixpath.normpath(posixpath.join(posixpath.dirname(ncx),urllib.parse.unquote(parts.path)))
                    if name in documents:
                        destination=name+('#'+parts.fragment if parts.fragment else '')
                        output_labels.setdefault(title,set()).add(destination)
        # Inferred names need evidence from the specific target, not the EPUB.
        source_inventory = recover_chm.source_pages(root, work)
        source_text = {p['path']:re.sub(r'\s+','',p['expected']) for p in source_inventory}
        text_cache = {}
        def matches_source(original, names):
            key=tuple(names)
            if key not in text_cache:
                text_cache[key]=mapped_text(names, documents)
            expected=source_text.get(original,'')
            return bool(expected) and expected in text_cache[key]
        for title,originals in source_labels.items():
            outputs=output_labels.get(title,set())
            if len(originals)==len(outputs)==1:
                original=next(iter(originals));name=next(iter(outputs))
                if original not in mapping:
                    unsplit=re.sub(r'_split_\d+(?=\.[^.]+$)','',posixpath.basename(name),flags=re.I)
                    names=([name] if '#' in name else sorted(by_split.get(unsplit.casefold(),[])) or [name])
                    if not matches_source(original,names):
                        raise ValueError('unverifiable converted CHM chapter boundary: '+original+' -> '+name)
                    assign(original,names)
        # Native Calibre output can rename unlisted pages. Accept a unique text
        # match in the reading spine, recording a concrete target for later gates.
        # Generated sections remain separate candidates even in a merged member.
        section_members={v.split('#',1)[0] for values in sections.values() for v in values}
        candidates=[[v] for values in sections.values() for v in values]
        candidates.extend([name] for name in spine_names if name in documents and name not in section_members)
        for page in source_inventory:
            name=page['path']
            if name not in mapping:
                matches=[values for values in candidates if matches_source(name,values)
                         and source_text[name]==text_cache[tuple(values)]]
                if len(matches)==1:
                    assign(name,matches[0])
        # Calibre may produce a valid EPUB with misdecoded or omitted text.
        # Every nonempty source page must pass against its mapped section(s).
        mismatches = source_text_mismatches(source_inventory, mapping, documents)
        if mismatches:
            detail = ", ".join(f"{name} ({expected}/{actual} chars)"
                                for name, expected, actual in mismatches[:5])
            raise ValueError("converted CHM EPUB text differs from source pages: " + detail)
    navigation_report=repair_epub(target,root,mapping)
    images=chm_resources.repair_images(target,root,mapping)
    links=chm_resources.repair_links(target,root,mapping)
    return {'navigation':navigation_report,'images':images,'links':links}


def mapped_region(target, documents):
    """Resolve a unique body/fragment subtree; a bare heading proves no siblings."""
    name, sep, fragment = target.partition('#')
    document = documents.get(name)
    if document is None:
        return None
    body = document.find('{*}body')
    if body is None or not sep:
        return body
    nodes = body.xpath('descendant-or-self::*[@id=$fragment or @name=$fragment]',
                       fragment=urllib.parse.unquote(fragment))
    return nodes[0] if len(nodes) == 1 else None


def mapped_text(targets, documents):
    """Read only mapped bodies or fragment subtrees, never neighbouring sections."""
    parts = []
    for target in targets:
        region = mapped_region(target, documents)
        if region is None:
            return ''
        parts.append(re.sub(r'\s+', '', ''.join(region.itertext())))
    return ''.join(parts)


def source_text_mismatches(source_pages, mapping, documents):
    """Return source pages whose text is altered, lost, or omitted from their targets."""
    mismatches = []
    for page in source_pages:
        source = page.get('path')
        expected = re.sub(r'\s+', '', page.get('expected') or '')
        targets = mapping.get(source, [])
        if not expected:
            continue
        actual = mapped_text(targets, documents)
        if expected not in actual:
            mismatches.append((source, len(expected), len(actual)))
    return mismatches
