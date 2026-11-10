"""Build a bounded verified EPUB chapter candidate locally, never publish it.

Only a newly captured exact plan is accepted. Source bytes must match the
pinned LFS SHA or Git blob, and old route/index identities remain unchanged.
No credentials are passed to the EPUB parser and no bucket object is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import resource
from pathlib import Path
import re
import zipfile
import urllib.error
import urllib.parse
import urllib.request

import httpx
from huggingface_hub import HfApi, HfFileSystem

try:
    from . import epub_chapters, reader_assets, reader_epub_plan, reader_format_backlog as backlog, search_snapshot
    from . import reader_lifecycle
    from .report_office_chapter_coverage import verify_bundle_payload
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import epub_chapters
    import reader_assets
    import reader_epub_plan
    import reader_format_backlog as backlog
    import search_snapshot
    import reader_lifecycle
    from report_office_chapter_coverage import verify_bundle_payload
    from reader_bucket_store import HubBucketStore


MAX_SOURCE = 128 * 1024 * 1024
MAX_FILES = 12_000
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_MEMBER = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_EXPANDED = 512 * 1024 * 1024
PARSER_FILE_LIMIT = MAX_MEMBER
PARSER_CPU_SECONDS = 600
PARSER_ADDRESS_BYTES = 4 * 1024 * 1024 * 1024
ALLOWED = frozenset({'huggingface.co', 'us.aws.cdn.hf.co', 'cdn-lfs.hf.co', 'cdn-lfs-us-1.hf.co',
                     'cas-bridge.xethub.hf.co', 'cas-server.xethub.hf.co',
                     'transfer.xethub.hf.co'})


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _check_url(newurl)
        result = super().redirect_request(request, fp, code, message, headers, newurl)
        if result and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(request.full_url).hostname:
            result.remove_header('Authorization')
        return result


def _check_url(url):
    value = urllib.parse.urlsplit(url)
    if (value.scheme != 'https' or value.hostname not in ALLOWED or value.username or value.password
            or value.port not in (None, 443)):
        raise ValueError('EPUB source redirect is outside approved HTTPS hosts')


def download(url, target, token):
    """Stream an entire source with a strict compressed-byte cap."""
    _check_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'reader-epub-shadow/1', 'Accept-Encoding': 'identity'})
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    opener = urllib.request.build_opener(_Redirect())
    count = 0
    with opener.open(request, timeout=120) as response, target.open('xb') as output:
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdigit() or int(length) > MAX_SOURCE):
            raise ValueError('EPUB source exceeds bounded content length')
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > MAX_SOURCE:
                raise ValueError('EPUB source exceeds bounded stream length')
            output.write(chunk)
    if count < 1:
        raise ValueError('empty EPUB source')


def source_identity(path: Path, row: dict) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_SOURCE:
        raise ValueError('invalid bounded EPUB source')
    digest, blob, count = hashlib.sha256(), hashlib.sha1(), 0
    blob.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            count += len(chunk); digest.update(chunk); blob.update(chunk)
    sha, git = digest.hexdigest(), blob.hexdigest()
    if row['reason'] == 'content_changed':
        if row['proof'] == 'current-lfs-sha256':
            if sha != row['current_source_sha256']:
                raise ValueError('downloaded EPUB differs from pinned LFS bytes')
        elif row['proof'] == 'exact-revision-git-blobs':
            if git != row['current_git_blob']:
                raise ValueError('downloaded EPUB differs from pinned Git blob')
        else:
            raise ValueError('unverified EPUB content-change proof')
        if sha == row['previous_source_sha256']:
            raise ValueError('EPUB content is unchanged despite candidate proof')
    elif row['reason'] == 'not_mapped':
        if row.get('proof') != 'unmapped-lfs-sha256' or sha != row.get('current_source_sha256'):
            raise ValueError('downloaded unmapped EPUB differs from pinned LFS bytes')
    else:
        raise ValueError('unsupported EPUB candidate reason')
    return {'sha256': sha, 'git_blob': git, 'bytes': count}


def _safe_receipt(plan):
    """Project only immutable, non-secret source identities needed to resume."""
    fields=('key','repo','path','source_revision','reason','proof','previous_revision',
            'previous_source_sha256','current_source_sha256','previous_git_blob','current_git_blob','previous_manifest')
    rows=[]
    for source in plan['sources']:
        if not isinstance(source,dict) or set(source)!=set(fields):
            raise ValueError('EPUB plan source schema differs from the exact selection')
        if source['reason']=='content_changed':
            if (not re.fullmatch(r'[0-9a-f]{40}',str(source['previous_revision'] or ''))
                    or not re.fullmatch(r'[0-9a-f]{64}',str(source['previous_source_sha256'] or ''))
                    or source['previous_revision']==source['source_revision']
                    or not isinstance(source['previous_manifest'],str) or not source['previous_manifest']):
                raise ValueError('EPUB changed-source parent identity is invalid')
            if source['proof']=='current-lfs-sha256':
                if (not re.fullmatch(r'[0-9a-f]{64}',str(source['current_source_sha256'] or ''))
                        or source['current_source_sha256']==source['previous_source_sha256']
                        or source['previous_git_blob'] is not None or source['current_git_blob'] is not None):
                    raise ValueError('EPUB changed-source LFS proof is invalid')
            elif source['proof']=='exact-revision-git-blobs':
                if (source['current_source_sha256'] is not None
                        or not re.fullmatch(r'[0-9a-f]{40}',str(source['previous_git_blob'] or ''))
                        or not re.fullmatch(r'[0-9a-f]{40}',str(source['current_git_blob'] or ''))
                        or source['previous_git_blob']==source['current_git_blob']):
                    raise ValueError('EPUB changed-source Git proof is invalid')
            else:
                raise ValueError('EPUB changed-source proof is invalid')
        elif source['reason']=='not_mapped':
            if (source['proof'] != 'unmapped-lfs-sha256'
                    or not isinstance(source['current_source_sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', source['current_source_sha256'])
                    or any(source[name] is not None for name in ('previous_revision', 'previous_source_sha256',
                        'previous_git_blob', 'current_git_blob', 'previous_manifest'))):
                raise ValueError('unmapped EPUB lacks exact LFS proof or claims prior evidence')
        else:
            raise ValueError('EPUB source reason is invalid')
        rows.append({field:source[field] for field in fields})
    return rows


def _verify_plan(plan):
    if (not isinstance(plan, dict) or plan.get('version') != 1 or plan.get('kind') != 'reader-epub-exact-build-plan'
            or plan.get('publication_ready') is not False or plan.get('remote_writes') != 0
            or plan.get('external_writers_verified') is not False or plan.get('profile') != reader_epub_plan.builder.EPUB_PROFILE
            or type(plan.get('shard')) is not int or not 0 <= plan['shard'] < 8
            or not isinstance(plan.get('sources'), list) or not 1 <= len(plan['sources']) <= 3
            or not re.fullmatch(r'[0-9a-f]{32}', str(plan.get('operation', '')))):
        raise ValueError('invalid exact EPUB shadow plan')
    allowed={'version','kind','operation','shard','search_generation','reader_generation',
             'canonical_index_sha256','shard_index_sha256','source_revisions_sha256','profile','max_attempts',
             'sources','remote_writes','publication_ready','external_writers_verified','plan_sha256'}
    explicit = plan.get('selection_mode') == 'explicit-source-keys-v1'
    if (set(plan) not in (allowed, allowed | {'selection_offset'}, allowed | {'selection_mode'})
            or ('selection_mode' in plan and not explicit)
            or ('selection_offset' in plan and (type(plan['selection_offset']) is not int
                                               or not 0 < plan['selection_offset'] <= 100_000))
            or type(plan.get('max_attempts')) is not int
            or (plan['max_attempts'] not in ((1,2,3) if explicit else (1,3)))
            or len(plan['sources'])>plan['max_attempts']
            or (explicit and len(plan['sources'])!=plan['max_attempts'])
            or any(not isinstance(plan.get(field),str) or not re.fullmatch(r'[0-9a-f]{64}',plan[field])
                   for field in ('search_generation','reader_generation','canonical_index_sha256',
                                 'shard_index_sha256','source_revisions_sha256','plan_sha256'))):
        raise ValueError('EPUB exact plan schema or identity differs')
    expected = hashlib.sha256(search_snapshot.canonical({k: v for k, v in plan.items() if k != 'plan_sha256'})).hexdigest()
    if expected != plan.get('plan_sha256'):
        raise ValueError('EPUB shadow plan identity differs')
    keys = set()
    for row in plan['sources']:
        if (not isinstance(row, dict) or not isinstance(row.get('repo'), str) or not isinstance(row.get('path'), str)
                or backlog.reader_route_plan.source_key(row['repo'], row['path']) != row.get('key')
                or row['key'] in keys or not row['path'].lower().endswith('.epub')
                or not re.fullmatch(r'[0-9a-f]{40}', str(row.get('source_revision', '')))
                or reader_epub_plan.builder.shard_for(row['key'], 8) != plan['shard']):
            raise ValueError('invalid EPUB shadow source identity')
        keys.add(row['key'])
    _safe_receipt(plan)
    if explicit and [row['key'] for row in plan['sources']] != sorted(keys):
        raise ValueError('explicit EPUB source keys are not in canonical order')
    return plan


def validate_archive(source: Path) -> None:
    """Bound ZIP structure before any parser reads or decompresses members."""
    total, names = 0, set()
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError('EPUB ZIP entry count exceeds limit')
        for member in members:
            name = member.filename
            if (not name or name.startswith('/') or '\\' in name or '\0' in name
                    or any(part in {'', '.', '..'} for part in name.rstrip('/').split('/'))
                    or name in names or member.flag_bits & 1):
                raise ValueError('invalid or duplicate EPUB ZIP member')
            names.add(name)
            # Git-supplied EPUB links are not allowed to escape the input bundle.
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError('EPUB ZIP contains a symbolic link')
            if member.file_size < 0 or member.file_size > MAX_MEMBER:
                raise ValueError('EPUB ZIP member exceeds limit')
            total += member.file_size
            if total > MAX_ARCHIVE_EXPANDED:
                raise ValueError('EPUB ZIP expands beyond limit')


def _bundle_index(root: Path, manifest: dict) -> tuple[list[dict], str]:
    reader_assets.validate_chapter_manifest(manifest)
    rows, total = [], 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('EPUB bundle contains a symbolic link')
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if ('\\' in relative or any(part in {'', '.', '..'} for part in relative.split('/'))
                or len(rows) >= MAX_FILES):
            raise ValueError('EPUB bundle path/count invalid')
        size = path.stat().st_size
        if size < 1 or size > MAX_MEMBER:
            raise ValueError('EPUB bundle member size invalid')
        total += size
        if total > MAX_OUTPUT_BYTES:
            raise ValueError('EPUB bundle exceeds output limit')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append({'path': relative, 'bytes': size, 'sha256': digest})
    by_path = {row['path']: row for row in rows}
    if 'chapter-manifest.json' not in by_path:
        raise ValueError('EPUB chapter manifest was not produced')
    expected = list(manifest['chapters'])
    if manifest.get('search_index'):
        expected.append(manifest['search_index'])
    for entry in expected:
        actual = by_path.get(entry['path'])
        if not actual or actual['bytes'] != entry['bytes'] or actual['sha256'] != entry['sha256']:
            raise ValueError('EPUB manifest member differs from generated bytes')
    if any(path.endswith('.epub') for path in by_path):
        raise ValueError('EPUB shadow bundle must not contain a full native book')
    search = manifest.get('search_index')
    if not isinstance(search, dict):
        raise ValueError('EPUB bundle has no complete search index')
    verify_bundle_payload({'chapter_count': len(manifest['chapters'])},
                          (root / 'chapter-manifest.json').read_bytes(),
                          (root / search['path']).read_bytes(),
                          (root / manifest['chapters'][0]['path']).read_bytes())
    return rows, epub_chapters.bundle_version(root)


def run_parser(source: Path, output: Path) -> None:
    """Do not expose tokens to parser code processing untrusted EPUB bytes."""
    script = Path(__file__).resolve()
    allowed = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'PYTHONPATH')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env['PYTHONHASHSEED'] = '0'
    env.pop('PYTHONPATH', None)
    subprocess.run([sys.executable, '-B', str(script), '--_parse', str(source), str(output)],
                   check=True, timeout=600, env=env)


def build(plan, output: Path, *, token=None, fetch=download, parse=run_parser):
    _verify_plan(plan)
    if output.exists() or output.is_symlink():
        raise ValueError('EPUB shadow output already exists')
    output.mkdir(parents=True, mode=0o700)
    report = []
    for number, row in enumerate(plan['sources']):
        work = output / str(number)
        work.mkdir()
        source = work / 'source.epub'
        fetch(reader_assets.source_url(row['repo'], row['source_revision'], row['path']), source, token)
        identity = source_identity(source, row)
        validate_archive(source)
        parse(source, work / 'bundle')
        manifest_path = work / 'bundle' / 'chapter-manifest.json'
        if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('EPUB shadow manifest is missing or oversized')
        manifest = json.loads(manifest_path.read_bytes())
        files, version = _bundle_index(work / 'bundle', manifest)
        # Keep raw source and generated files only under the requested local
        # output; neither the old Reader root nor the bucket is changed.
        report.append({'key': row['key'], 'source_revision': row['source_revision'], 'reason': row['reason'],
                       'source': identity, 'previous_manifest': row['previous_manifest'],
                       'bundle_version': version, 'chapter_count': len(manifest['chapters']), 'files': files})
    return {'version': 1, 'kind': 'reader-epub-local-shadow', 'operation': plan['operation'],
            'plan_sha256': plan['plan_sha256'], 'search_generation': plan['search_generation'],
            'reader_generation': plan['reader_generation'], 'sources': report, 'remote_writes': 0,
            'publication_ready': False, 'external_writers_verified': False}



def staging_plan(plan: dict, report: dict, output: Path) -> dict:
    """Bind every locally verified bundle file to a unique protection record.

    This only generates a local preview. It neither creates a protection nor
    uploads data or advances an index. Existing Reader routes stay untouched.
    """
    _verify_plan(plan)
    if (not isinstance(report, dict) or report.get('kind') != 'reader-epub-local-shadow'
            or report.get('operation') != plan['operation'] or report.get('plan_sha256') != plan['plan_sha256']
            or report.get('search_generation') != plan['search_generation']
            or report.get('reader_generation') != plan['reader_generation']
            or report.get('remote_writes') != 0 or report.get('publication_ready') is not False
            or not isinstance(report.get('sources'), list) or len(report['sources']) != len(plan['sources'])):
        raise ValueError('EPUB shadow report differs from exact plan')
    root = output.resolve()
    if output.is_symlink() or not output.is_dir():
        raise ValueError('EPUB shadow output must be an ordinary directory')
    actions, paths = [], set()
    for number, (source_plan, source_report) in enumerate(zip(plan['sources'], report['sources'])):
        if (not isinstance(source_report, dict) or source_report.get('key') != source_plan['key']
                or source_report.get('source_revision') != source_plan['source_revision']
                or source_report.get('previous_manifest') != source_plan['previous_manifest']):
            raise ValueError('EPUB shadow source differs from plan')
        work = root / str(number)
        if work.is_symlink() or not work.is_dir():
            raise ValueError('EPUB shadow source directory is invalid')
        local_source = work / 'source.epub'
        identity = source_identity(local_source, source_plan)
        if source_report.get('source') != identity:
            raise ValueError('EPUB source identity changed after shadow build')
        bundle = work / 'bundle'
        if bundle.is_symlink() or not bundle.is_dir():
            raise ValueError('EPUB shadow bundle directory is invalid')
        manifest_path = bundle / 'chapter-manifest.json'
        if (manifest_path.is_symlink() or not manifest_path.is_file()
                or manifest_path.stat().st_size > 16 * 1024 * 1024):
            raise ValueError('EPUB shadow manifest is missing or oversized')
        manifest = json.loads(manifest_path.read_bytes())
        files, version = _bundle_index(bundle, manifest)
        if (files != source_report.get('files') or version != source_report.get('bundle_version')
                or len(manifest['chapters']) != source_report.get('chapter_count')):
            raise ValueError('EPUB shadow bundle changed after verification')
        remote_root = f"chapters/ebook/epub/{identity['sha256']}/{version}"
        if not reader_lifecycle.valid_processing_root(reader_assets.READER_ASSETS_BUCKET, remote_root):
            raise ValueError('EPUB candidate has invalid processing root')
        protection = f"reader-index/processing/epub-exact-{plan['operation']}-{number:02d}.json"
        uploads = []
        for row in files:
            remote = remote_root + '/' + row['path']
            if remote in paths:
                raise ValueError('EPUB staging plan has duplicate remote objects')
            paths.add(remote)
            uploads.append({**row, 'remote': remote})
        actions.append({'key': source_plan['key'], 'source_revision': source_plan['source_revision'],
                        'source_sha256': identity['sha256'], 'previous_manifest': source_plan['previous_manifest'],
                        'root': remote_root, 'protection_path': protection, 'files': uploads})
    body = {'version': 1, 'kind': 'reader-epub-immutable-staging-preview', 'operation': plan['operation'],
            'plan_sha256': plan['plan_sha256'], 'search_generation': plan['search_generation'],
            'reader_generation': plan['reader_generation'], 'actions': actions, 'remote_writes': 0,
            'index_written': False, 'publication_ready': False, 'external_writers_verified': False}
    body['staging_sha256'] = hashlib.sha256(search_snapshot.canonical(body)).hexdigest()
    return body

def bound_staging_plan(plan: dict, report: dict, output: Path, *, validate_source=None) -> dict:
    """Verify a protected bundle without retaining its native EPUB in the artifact.

    The report's source identity must already be independently tied to the live
    immutable source by a caller performing fresh network reads. This helper
    verifies local converted bytes; it does not itself fetch or publish data.
    """
    _verify_plan(plan)
    if not callable(validate_source):
        raise ValueError('fresh EPUB source revalidation is required')
    validate_source(plan)
    if (not isinstance(report,dict) or report.get('kind')!='reader-epub-local-shadow'
            or report.get('operation')!=plan['operation'] or report.get('plan_sha256')!=plan['plan_sha256']
            or report.get('search_generation')!=plan['search_generation']
            or report.get('reader_generation')!=plan['reader_generation'] or report.get('remote_writes')!=0
            or report.get('publication_ready') is not False or report.get('external_writers_verified') is not False
            or not isinstance(report.get('sources'),list) or len(report['sources'])!=len(plan['sources'])):
        raise ValueError('EPUB shadow report differs from exact plan')
    if output.is_symlink() or not output.is_dir():raise ValueError('EPUB bundle output must be an ordinary directory')
    root=output.resolve();actions=[];paths=set()
    for number,(source,observed) in enumerate(zip(plan['sources'],report['sources'])):
        if (not isinstance(observed,dict) or observed.get('key')!=source['key']
                or observed.get('source_revision')!=source['source_revision']
                or observed.get('previous_manifest')!=source['previous_manifest']):
            raise ValueError('EPUB shadow source differs from plan')
        identity=observed.get('source')
        if (not isinstance(identity,dict) or set(identity)!={'sha256','git_blob','bytes'}
                or not isinstance(identity['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',identity['sha256'])
                or not isinstance(identity['git_blob'],str) or not re.fullmatch(r'[0-9a-f]{40}',identity['git_blob'])
                or type(identity['bytes']) is not int or not 0<identity['bytes']<=MAX_SOURCE):
            raise ValueError('invalid EPUB source byte identity')
        if source['reason']=='content_changed':
            if identity['sha256']==source['previous_source_sha256']:
                raise ValueError('EPUB source was not actually changed')
            if source['proof']=='current-lfs-sha256' and identity['sha256']!=source['current_source_sha256']:
                raise ValueError('EPUB source differs from pinned LFS proof')
            if source['proof']=='exact-revision-git-blobs' and identity['git_blob']!=source['current_git_blob']:
                raise ValueError('EPUB source differs from pinned Git proof')
        elif identity['sha256']!=source['current_source_sha256']:
            raise ValueError('unmapped EPUB source differs from pinned LFS proof')
        work=root/str(number);bundle=work/'bundle'
        if work.is_symlink() or not work.is_dir() or bundle.is_symlink() or not bundle.is_dir():
            raise ValueError('EPUB bundle directory is invalid')
        manifest_path=bundle/'chapter-manifest.json'
        if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size>16*1024*1024:
            raise ValueError('EPUB manifest is missing or oversized')
        manifest=json.loads(manifest_path.read_bytes());files,version=_bundle_index(bundle,manifest)
        if (files!=observed.get('files') or version!=observed.get('bundle_version')
                or len(manifest['chapters'])!=observed.get('chapter_count')):
            raise ValueError('EPUB bundle changed after conversion')
        remote_root=f"chapters/ebook/epub/{identity['sha256']}/{version}"
        if not reader_lifecycle.valid_processing_root(reader_assets.READER_ASSETS_BUCKET,remote_root):
            raise ValueError('EPUB candidate has invalid processing root')
        protection=f"reader-index/processing/epub-exact-{plan['operation']}-{number:02d}.json";uploads=[]
        for row in files:
            remote=remote_root+'/'+row['path']
            if remote in paths:raise ValueError('EPUB staging plan has duplicate remote objects')
            paths.add(remote);uploads.append({**row,'remote':remote})
        actions.append({'key':source['key'],'source_revision':source['source_revision'],
                        'source_sha256':identity['sha256'],'previous_manifest':source['previous_manifest'],
                        'root':remote_root,'protection_path':protection,'files':uploads})
    body={'version':1,'kind':'reader-epub-immutable-staging-preview','operation':plan['operation'],
          'plan_sha256':plan['plan_sha256'],'search_generation':plan['search_generation'],
          'reader_generation':plan['reader_generation'],'actions':actions,'remote_writes':0,
          'index_written':False,'publication_ready':False,'external_writers_verified':False}
    body['staging_sha256']=hashlib.sha256(search_snapshot.canonical(body)).hexdigest()
    validate_source(plan)
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    token = os.environ.get('HF_TOKEN')
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        plan = reader_epub_plan.inspect(hub=HubBucketStore(), api=HfApi(token=token), fs=HfFileSystem(token=token),
                                        client=client, shard=args.shard, operation=os.urandom(16).hex())
        report = build(plan, args.output, token=token)
        # Freshly inspect after local conversion. A different selection or source
        # identity cannot be accepted as a reproducible current shadow.
        stage_preview = staging_plan(plan, report, args.output)
        current = reader_epub_plan.inspect(hub=HubBucketStore(), api=HfApi(token=token), fs=HfFileSystem(token=token),
                                           client=client, shard=args.shard, operation=plan['operation'])
        if current != plan:
            raise ValueError('EPUB plan changed during local shadow build')
    target = args.output / 'report.json'
    with target.open('x', encoding='utf8') as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2);stream.write('\n')
    with (args.output / 'staging-preview.json').open('x', encoding='utf8') as stream:
        json.dump(stage_preview, stream, ensure_ascii=False, sort_keys=True, indent=2);stream.write('\n')
    print(json.dumps({'kind': report['kind'], 'plan_sha256': report['plan_sha256'],
                      'sources': len(report['sources']), 'remote_writes': 0, 'publication_ready': False},
                     ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--_parse':
        resource.setrlimit(resource.RLIMIT_FSIZE, (PARSER_FILE_LIMIT, PARSER_FILE_LIMIT))
        resource.setrlimit(resource.RLIMIT_CPU, (PARSER_CPU_SECONDS, PARSER_CPU_SECONDS + 1))
        resource.setrlimit(resource.RLIMIT_AS, (PARSER_ADDRESS_BYTES, PARSER_ADDRESS_BYTES))
        epub_chapters.build_bundle(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        raise SystemExit(main())
