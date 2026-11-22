"""Load a SHA-pinned Office release ZIP only in the central production job."""
import argparse
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from . import publish_reader_v3 as v3, reader_exact_office_publish as publication
from .reader_image_history_store import S3ClaimStore
from .reader_web_exact_plan import _is_sha

PREFIX='reader-index/gc/office-exact-inputs/v1/'
MAX_ZIP=16*1024*1024
BASE={'review.json','index.json','sidecar-before.gz','sidecar-after.gz','pointer.json'}
FILES={'chapter-manifest.json','epub-search-index.json.gz','chapters/chapter-0001.xhtml','chapters/chapter-0002.xhtml',
       'resources/chapter-0002/page_styles.css','resources/chapter-0002/stylesheet.css'}
ALLOWED=BASE|{f'{ext}/{name}' for ext in ('rtf','docx') for name in FILES}


def unpack(blob, digest, root):
    if not _is_sha(digest) or not 0<len(blob)<=MAX_ZIP or publication.plan.digest(blob)!=digest:
        raise ValueError('Office input ZIP SHA or length differs')
    with ZipFile(io.BytesIO(blob)) as z:
        infos=z.infolist()
        if (len(infos)!=len(ALLOWED) or {item.filename for item in infos}!=ALLOWED
                or sum(item.file_size for item in infos)>24*1024*1024):
            raise ValueError('Office input ZIP contains unexpected members')
        for item in infos:
            with z.open(item) as stream: data=stream.read(item.file_size+1)
            if len(data)!=item.file_size or (not data and not item.filename.endswith('/page_styles.css')):
                raise ValueError('Office ZIP member size changed')
            file=root/item.filename;file.parent.mkdir(parents=True,exist_ok=True)
            with file.open('xb') as f:f.write(data)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--resource-json',required=True);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args(); opts=json.loads(a.resource_json)
    if (not isinstance(opts,dict) or set(opts)!={'archive_sha256','review_sha256','phase','external_writers_stopped'}
            or not all(_is_sha(opts[k]) for k in ('archive_sha256','review_sha256'))
            or opts['phase'] not in {'apply','resume-index','resume-legacy','observe-v3'}
            or opts['external_writers_stopped'] is not True):
        raise ValueError('Office exact review identity, explicit phase and writer confirmation required')
    publication.guard_window(v3.CentralHubStore(),True)
    blob,_=S3ClaimStore()._get(PREFIX+opts['archive_sha256']+'.zip',maximum=MAX_ZIP)
    if blob is None:
        raise ValueError('approved Office review ZIP is not staged')
    with TemporaryDirectory(prefix='office-report-exact-') as name:
        root=Path(name);unpack(blob,opts['archive_sha256'],root)
        result=publication.run(root,opts['review_sha256'],phase=opts['phase'],external_writers_stopped=True)
    with a.output.open('xb') as f:f.write(v3.encode(result))

if __name__=='__main__':main()
