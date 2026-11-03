"""Repair missing local EPUB images only through unambiguous CHM source paths."""
import hashlib
import io
import posixpath
import re
import urllib.parse
import zipfile
from lxml import etree
from PIL import Image
try:
    from .chm_navigation import SourcePaths
except ImportError:
    from chm_navigation import SourcePaths


def repair_images(path, root, mapping):
    with zipfile.ZipFile(path) as archive:
        infos={i.filename:i for i in archive.infolist()}
        content={n:archive.read(n) for n in infos}
    paths=SourcePaths(content)
    source_paths=SourcePaths(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
    reverse={}
    for source, outputs in mapping.items():
        for output in outputs:
            reverse.setdefault(output.split('#',1)[0],[]).append(source)
    opf=etree.fromstring(content['META-INF/container.xml']).find('.//{*}rootfile').get('full-path')
    package=etree.fromstring(content[opf]);manifest=package.find('{*}manifest')
    ns=etree.QName(manifest).namespace
    changes=[];missing=[];added={}
    for name in list(content):
        if not name.lower().endswith(('.htm','.html','.xhtml')):continue
        doc=etree.fromstring(content[name]);dirty=False
        for node in doc.xpath('//*[local-name()="img" or local-name()="image" or local-name()="a"]'):
            tag=etree.QName(node).localname
            attr='src' if tag=='img' else next((k for k in node.attrib if etree.QName(k).localname=='href'),'href')
            raw=node.get(attr,'')
            if not raw:continue
            if tag=='a' and not re.search(r'\.(?:png|jpe?g|gif|webp)(?:;\d+)?$',urllib.parse.unquote(urllib.parse.urlsplit(raw).path),re.I):continue
            target,fragment,status=paths.resolve(name,raw)
            if status in ['exact','external']:continue
            if status=='casefold':
                node.set(attr,urllib.parse.quote(posixpath.relpath(target,posixpath.dirname(name) or '.'),safe='/'))
                dirty=True;changes.append({'page':name,'src':raw,'kind':'path_case'});continue
            candidates=[]
            for source in reverse.get(name,[]):
                resolved,_,state=source_paths.resolve(source,raw)
                if resolved:candidates.append(resolved)
            if not candidates:
                resolved,_,state=source_paths.resolve('',raw)
                if resolved:candidates.append(resolved)
            candidates=list(dict.fromkeys(candidates))
            hashes={hashlib.sha256((root/c).read_bytes()).hexdigest() for c in candidates}
            if len(hashes)!=1:
                missing.append({'page':name,'src':raw,'reason':'ambiguous_source' if candidates else 'missing_source'});continue
            data=(root/candidates[0]).read_bytes()
            try:
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    mime=Image.MIME.get(image.format)
                    if mime not in {'image/png','image/jpeg','image/gif','image/webp'}:
                        buffer=io.BytesIO();image.convert('RGBA').save(buffer,format='PNG');data=buffer.getvalue();mime='image/png'
            except Exception:
                missing.append({'page':name,'src':raw,'reason':'invalid_source_image','source':candidates[0]});continue
            digest=hashlib.sha256(data).hexdigest()
            target=posixpath.join(posixpath.dirname(opf),'chm-images',digest+'.'+{'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp'}[mime])
            if target not in added:
                added[target]=data
                uid='chm-image-'+digest
                etree.SubElement(manifest,'{'+ns+'}item',id=uid,href=urllib.parse.quote(posixpath.relpath(target,posixpath.dirname(opf) or '.'),safe='/'),**{'media-type':mime})
            node.set(attr,urllib.parse.quote(posixpath.relpath(target,posixpath.dirname(name) or '.'),safe='/'))
            changes.append({'page':name,'src':raw,'source':candidates[0],'kind':'restored'})
            dirty=True
        if dirty:content[name]=etree.tostring(doc,encoding='utf-8',xml_declaration=True)
    if changes:
        content.update(added);content[opf]=etree.tostring(package,encoding='utf-8',xml_declaration=True)
        temporary=path.with_suffix('.images.epub')
        with zipfile.ZipFile(temporary,'w') as archive:
            for name in sorted(content,key=lambda n:n!='mimetype'):
                if name in infos:archive.writestr(infos[name],content[name])
                else:archive.writestr(name,content[name],compress_type=zipfile.ZIP_DEFLATED)
        temporary.replace(path)
    return {'changes':changes,'missing':missing,'added':len(added)}


def repair_links(path, root, mapping):
    with zipfile.ZipFile(path) as archive:
        infos={i.filename:i for i in archive.infolist()}
        content={n:archive.read(n) for n in infos}
    documents={n:etree.fromstring(data) for n,data in content.items() if n.lower().endswith(('.htm','.html','.xhtml'))}
    anchors={n:set(d.xpath('//@id | //@name')) for n,d in documents.items()}
    files=SourcePaths(content)
    source_files=SourcePaths(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
    reverse={}
    for source,outputs in mapping.items():
        for output in outputs:reverse.setdefault(output.split('#',1)[0],[]).append(source)
    changes=[];missing=[]
    for name,doc in documents.items():
        dirty=False
        for link in doc.xpath('//*[local-name()="a"][@href]'):
            raw=link.get('href');dest,fragment,status=files.resolve(name,raw)
            fragment=urllib.parse.unquote(fragment)
            if status=='external':continue
            if dest and (not fragment or fragment in anchors.get(dest,set())):
                if status=='exact':continue
                targets={dest+('#'+fragment if fragment else '')}
            else:
                targets=set()
                for source in reverse.get(name,[]):
                    original,original_fragment,state=source_files.resolve(source,raw)
                    if original not in mapping:continue
                    values=mapping[original]
                    original_fragment=urllib.parse.unquote(original_fragment)
                    if not original_fragment:targets.add(values[0]);continue
                    for value in values:
                        member,_,prefix=value.partition('#')
                        for candidate in [original_fragment,urllib.parse.quote(original_fragment,safe=''),
                                          re.sub(r'^_msoanchor_', '_anchor_', original_fragment),
                                          prefix+'--'+original_fragment if prefix else original_fragment]:
                            if candidate in anchors.get(member,set()):targets.add(member+'#'+candidate)
            if len(targets)!=1:
                missing.append({'page':name,'href':raw,'reason':'ambiguous' if targets else 'unresolved'});continue
            destination=targets.pop();member,sep,fragment=destination.partition('#')
            rewritten=urllib.parse.quote(posixpath.relpath(member,posixpath.dirname(name) or '.'),safe='/')
            if sep:rewritten+='#'+urllib.parse.quote(fragment,safe='!$&\'()*+,;=:@/?')
            if rewritten!=raw:
                link.set('href',rewritten);dirty=True;changes.append({'page':name,'before':raw,'after':rewritten})
        if dirty:content[name]=etree.tostring(doc,encoding='utf-8',xml_declaration=True)
    if changes:
        temporary=path.with_suffix('.links.epub')
        with zipfile.ZipFile(temporary,'w') as archive:
            for n in sorted(content,key=lambda n:n!='mimetype'):archive.writestr(infos[n],content[n])
        temporary.replace(path)
    return {'changes':changes,'missing':missing}
