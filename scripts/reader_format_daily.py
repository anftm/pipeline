"""Bounded Reader format controller; workflow gates authorize known writers.

The parent must use concurrency group reader-format-daily (never reader-sidecar
or daily-sync), inspect first, and upload --journal and --output with always().
Producers accept automation_id, defer_unify=true and run-name
reader-auto/{automation_id}; media additionally accepts extension. Publication
accepts automation_id with the same run-name, but no defer_unify input.

An explicit, enabled JPG/MP3 canary attempts one ordinary batch and verifies
publication without queuing a continuation. Normal automation never boosts.

A journal is a local recovery artifact, not a distributed lock. Existing journals
are never replayed: observe only watches recorded operations and requires review.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid
from urllib.parse import urlsplit
import zipfile

try:
    from . import reader_ordinary_boost as boost
except ImportError:
    import reader_ordinary_boost as boost


REPOSITORY = 'anftm/pipeline'
PUBLISH = 'reader-v3-publish.yml'
DAILY = 'reader-format-daily.yml'
DAILY_NAME = 'Reader/桶 · Daily Format Conversion'
THRESHOLD = 1
MAX_BATCHES = 20
MAX_SECONDS = 5 * 60 * 60
# Shared with the serial Web profile publisher. Identical and held outcomes
# consume slots too; the existing managed bundle byte limits still apply.
WEB_PROFILE_BATCH_ROWS = 50
PUBLICATION_ARTIFACT = 'v3-publication-report'
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 16 * 1024 * 1024
MAX_REPORT = 2 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{40}\Z')
GENERATION = re.compile(r'[0-9a-f]{64}\Z')
OPERATION_ID = re.compile(r'[0-9a-f]{32}\Z')
# Explicit allowlist: never dispatch a workflow supplied by an input summary.
FAMILIES = {
    'image': ('jpg jpeg png bmp webp gif tif tiff', 'reader-image-stream.yml', 3),
    'ebook': ('epub mobi azw3 fb2', 'reader-ebook-stream.yml', 3),
    'chm': ('chm', 'reader-chm-repair.yml', 5),
    'office-chapters': ('doc docx odt rtf', 'reader-office-chapters.yml', 5),
    'spreadsheet': ('xls xlsx csv ods', 'reader-spreadsheet-stream.yml', 10),
    'web': ('html htm mht mhtml', 'reader-web-stream.yml', 10),
    'static-pdf': ('ppt pptx pps wps ps', 'reader-static-pdf-stream.yml', 10),
    'media': ('mp3 wav m4a flac mpga ape wma amr aac mp4 mov asx flv f4v rm rmvb mkv avi mpg mpeg mts ts wmv swf',
              'reader-media-stream.yml', 3),
    'text': ('txt md markdown vcf ini', 'reader-text-stream.yml', 10),
}
FORMATS = {ext: (family, workflow, limit) for family, (exts, workflow, limit) in FAMILIES.items()
           for ext in exts.split()}
COUNTS = ('sources', 'not_mapped', 'source_content_changed', 'source_content_unchanged',
          'source_identity_unknown', 'mapped_content_not_checked', 'recorded_failure_current',
          'pending_shard_checkpoint', 'profile_outdated', 'profile_upgrade_deferred', 'definite_backlog')
UNCERTAIN = ('source_identity_unknown', 'mapped_content_not_checked',
             'recorded_failure_current', 'pending_shard_checkpoint')


def validate_summary(summary):
    if (not isinstance(summary, dict) or summary.get('version') != 1
            or summary.get('kind') != 'reader-format-backlog'
            or type(summary.get('threshold')) is not int or summary['threshold'] != THRESHOLD):
        raise ValueError(f'requires a threshold-{THRESHOLD} reader-format-backlog summary')
    for field in ('search_generation', 'reader_generation'):
        if not isinstance(summary.get(field), str) or not GENERATION.fullmatch(summary[field]):
            raise ValueError('invalid ' + field)
    rows = summary.get('formats')
    if not isinstance(rows, list) or not rows:
        raise ValueError('formats inventory is missing')
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid format row')
        ext = row.get('extension')
        if not isinstance(ext, str) or ext in indexed:
            raise ValueError('invalid or duplicate extension')
        if ext in {'pdf', 'djvu'}:
            # Dataset-primary policy; never a conversion target.
            indexed[ext] = row
            continue
        if ext not in FORMATS:
            raise ValueError('unsupported format: ' + ext)
        family, workflow, _limit = FORMATS[ext]
        if row.get('family') != family or row.get('workflow') != workflow:
            raise ValueError('format workflow differs from allowlist: ' + ext)
        for field in COUNTS:
            if type(row.get(field)) is not int or not 0 <= row[field] <= row['sources']:
                raise ValueError('invalid count: ' + ext + '/' + field)
        if (row['definite_backlog'] != row['not_mapped'] + row['source_content_changed'] + row['profile_outdated']
                or row['profile_outdated'] + row['profile_upgrade_deferred'] > row['source_content_unchanged']
                or ((row['profile_outdated'] or row['profile_upgrade_deferred']) and family not in {'chm','web'})
                or sum(row[k] for k in ('not_mapped', 'source_content_changed', 'source_content_unchanged',
                                        'source_identity_unknown', 'mapped_content_not_checked')) != row['sources']):
            raise ValueError('inconsistent source counts: ' + ext)
        for field in ('all_source_identities_checked', 'conversion_dependencies_checked'):
            if type(row.get(field)) is not bool:
                raise ValueError('missing verification flag: ' + ext + '/' + field)
        indexed[ext] = row
    if not set(FORMATS).issubset(indexed):
        raise ValueError('formats inventory is incomplete')
    return indexed


# Accept durable receipts produced before the source-change policy was widened.
SOURCE_CHANGE_SCOPES = frozenset({'explicit_source_changes', 'first_conversion_only','source_or_web_profile_upgrade'})


def explicit_source_changes(row, *, triggered=False):
    """New and proven-changed sources recapture dependencies during conversion.

    Completion covers known source changes only. Dependency-only changes to
    unchanged mapped sources remain uncounted and are never implicitly retried.
    """
    return ((FORMATS[row['extension']][0] == 'web' or row['extension'] in {'md', 'markdown'})
            and (row['definite_backlog'] > 0 or triggered)
            and row['all_source_identities_checked'] is True
            and not any(row[field] for field in UNCERTAIN))


def dependency_report(row):
    checked = row['conversion_dependencies_checked'] is True
    return {'conversion_dependencies_checked': checked,
            'dependency_freshness': 'checked' if checked else 'unknown',
            'warnings': ([] if checked else [
                'Mapped unchanged sources have unknown external dependency freshness. '
                'Dependency-only changes to unchanged sources are not counted or refreshed; '
                'conversion recaptures dependencies only for new or proven-changed sources. ' +
                ('Completion covers known source changes and explicitly eligible profile upgrades only, not all dependency changes.'
                 if row['profile_outdated'] else 'Completion covers known source changes only, not all dependency changes.')])}


def blockers(row, *, source_changes_triggered=False, first_conversion_triggered=False):
    reasons = [field for field in UNCERTAIN if row[field]]
    if row['sources'] and row['all_source_identities_checked'] is not True:
        reasons.append('all_source_identities_checked is not true')
    dependency_only_unknown = (row['definite_backlog'] == 0
                               and (FORMATS[row['extension']][0] == 'web' or row['extension'] in {'md', 'markdown'}))
    if (row['sources'] and row['conversion_dependencies_checked'] is not True and not dependency_only_unknown
            and not explicit_source_changes(row, triggered=source_changes_triggered or first_conversion_triggered)):
        reasons.append('conversion_dependencies_checked is not true')
    if FORMATS[row['extension']][1] is None:
        reasons.append('no automatic workflow')
    return reasons


def plan(summary, *, boost_canary=None):
    rows = validate_summary(summary)
    decisions = {}
    for ext, row in sorted(rows.items()):
        if ext in {'pdf', 'djvu'}:
            decisions[ext] = {'state': 'excluded', 'reasons': ['existing dataset-primary policy']}
            continue
        if boost_canary is not None and ext != boost_canary:
            decisions[ext] = {'state': 'excluded', 'reasons': ['outside the requested boost canary']}
            continue
        reasons = blockers(row)
        state = ('blocked' if reasons else 'triggered' if row['definite_backlog'] > 0
                 else 'complete')
        decisions[ext] = {'state': state, 'reasons': reasons,
                          'counts': {key: row[key] for key in COUNTS}, 'workflow': FORMATS[ext][1],
                          'completion_scope': ('no_sources' if row['sources'] == 0 else
                                               'source_or_web_profile_upgrade' if FORMATS[ext][0]=='web' and row['profile_outdated'] else
                                               'explicit_source_changes' if not row['conversion_dependencies_checked']
                                               and explicit_source_changes(row) else 'definite_backlog'),
                          **dependency_report(row)}
    return {'version': 1, 'kind': 'reader-format-daily', 'mode': 'plan',
            'search_generation': summary['search_generation'], 'reader_generation': summary['reader_generation'],
            'formats': decisions, 'blocked': {ext: row['reasons'] for ext, row in decisions.items()
                                              if row['state'] == 'blocked'},
            'batches': 0, 'operation_ids': [], 'operations': [], 'status': 'planned'}


def producer_inputs(extension, automation_id, *, expected_sha, search_generation, boost_limit=None):
    family, _workflow, limit = FORMATS[extension]
    if (not _workflow or not OPERATION_ID.fullmatch(automation_id)
            or not SHA.fullmatch(expected_sha) or not GENERATION.fullmatch(search_generation)):
        raise ValueError('invalid producer operation')
    if boost_limit is not None:
        boost.byte_budget(extension, boost_limit)
        limit = boost_limit
    inputs = {'limit': str(limit), 'apply': 'true', 'automation_id': automation_id, 'defer_unify': 'true',
              'expected_sha': expected_sha, 'search_generation': search_generation}
    if family == 'chm':
        inputs.update(retry_failed='false', failed_only='false')
    else:
        inputs.update(extension=extension, retry_failures='false')
    if boost_limit is not None:
        inputs['boost_canary'] = 'true'
    if family == 'image':
        inputs['shard'] = 'all'
    if family == 'media':
        inputs['kind'] = ('audio' if extension in 'mp3 wav m4a flac mpga ape wma amr aac'.split()
                          else 'swf' if extension == 'swf' else 'video')
    return inputs


def save(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open('x', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            save(temporary, value, exclusive=True)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class WallTimeout(BaseException):
    """Not swallowed by Hub/network retries; handled at the controller boundary."""


class DispatchRejected(RuntimeError):
    """A definitive client rejection cannot be reported as an accepted continuation."""


class GitHub:
    """No POST retries. Every ambiguous dispatch is resolved only by observation."""
    def __init__(self, token, *, repository=REPOSITORY, session=None, local_head=None):
        if repository != REPOSITORY or not token:
            raise ValueError('requires pipeline repository and GitHub token')
        if session is None:
            import requests
            session = requests.Session()
        self.session, self.repository, self.local_head = session, repository, local_head
        self.session.headers.update({'Authorization': 'Bearer ' + token,
                                     'Accept': 'application/vnd.github+json',
                                     'X-GitHub-Api-Version': '2022-11-28'})

    def request(self, method, path, **kwargs):
        import requests
        for attempt in range(3):
            try:
                response = self.session.request(method, 'https://api.github.com/repos/' + self.repository + path,
                                                timeout=30, allow_redirects=False, **kwargs)
            except requests.exceptions.SSLError:
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                if method != 'GET' or attempt == 2:
                    raise
            else:
                if (method == 'GET' and response.status_code in {429, 500, 502, 503, 504}
                        and attempt < 2):
                    response.close()
                else:
                    if not 200 <= response.status_code < 300:
                        if method == 'POST' and response.status_code in {400, 401, 403, 404, 405, 409, 422}:
                            raise DispatchRejected('GitHub POST rejected: ' + str(response.status_code))
                        raise RuntimeError('GitHub ' + method + ' returned ' + str(response.status_code))
                    return response.json() if response.status_code != 204 else None
            time.sleep(0.5 * (2 ** attempt))

    def check_main(self, expected_sha):
        if self.local_head and self.local_head() != expected_sha:
            raise RuntimeError('local pipeline HEAD changed')
        if self.request('GET', '/git/ref/heads/main')['object']['sha'] != expected_sha:
            raise RuntimeError('pipeline main differs from expected HEAD')

    def runs(self, workflow=None, **params):
        path = '/actions/' + ('workflows/' + workflow + '/' if workflow else '') + 'runs'
        # Status counts and pages may briefly disagree while runs transition.
        # Fresh URLs and paced full observations avoid repeating the same cached
        # response. Never return a truncated inventory or retry a mutation.
        last_observation = None
        for attempt in range(6):
            if attempt:
                time.sleep(min(2 ** attempt, 16))
            observation = uuid.uuid4().hex
            result = {}
            expected_total = None
            for page in range(1, 11):
                data = self.request('GET', path,
                                    params={**params, 'per_page': 100, 'page': page,
                                            '_observation': observation},
                                    headers={'Cache-Control': 'no-cache'})
                rows, total = data.get('workflow_runs'), data.get('total_count')
                if not isinstance(rows, list) or type(total) is not int or not 0 <= total <= 1000:
                    raise RuntimeError('run inventory incomplete or exceeds observation bound')
                last_observation = {'page': page, 'total': total, 'rows': len(rows),
                                    'previous_total': expected_total, 'unique_before': len(result)}
                if expected_total is None:
                    expected_total = total
                elif expected_total != total:
                    break
                for row in rows:
                    result[row['id']] = row
                if page * 100 >= total:
                    if len(result) == total:
                        return list(result.values())
                    break
        raise RuntimeError('run inventory changed or is incomplete after bounded reads: '
                           + json.dumps({'workflow': workflow, 'filters': params,
                                         'last_observation': last_observation}, sort_keys=True))

    def active_children(self, parent_run_id=None):
        active = {}
        for status in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
            for row in self.runs(status=status):
                name = row.get('path', '').split('/')[-1].split('@')[0]
                if (str(row.get('id')) != str(parent_run_id)
                        and (name.startswith(('reader-', 'search-daily-', 'search-promotion-'))
                             or name == 'pdf-account-worker.yml')
                        and row.get('status') != 'completed'):
                    if (row.get('status') in {'pending', 'queued'}
                            and name in {DAILY, 'reader-backlog-monitor.yml', 'reader-maintenance.yml'}):
                        # These workflows share reader-format-daily concurrency.
                        # Only a job that has not started is a waiting successor,
                        # not a concurrently executing producer or observer.
                        jobs = self.request('GET', f'/actions/runs/{row["id"]}/jobs', params={'per_page': 100})
                        entries = jobs.get('jobs')
                        if (isinstance(entries, list) and type(jobs.get('total_count')) is int
                                and jobs['total_count'] == len(entries)
                                and all(j.get('status') in {'queued', 'pending', 'waiting'}
                                        and not j.get('steps') and not j.get('started_at') for j in entries)):
                            continue
                    if row.get('status') == 'queued':
                        jobs = self.request('GET', f'/actions/runs/{row["id"]}/jobs', params={'per_page': 1})
                        if isinstance(jobs, dict) and jobs.get('total_count') == 0 and jobs.get('jobs') == []:
                            continue
                    active[row['id']] = row
        return list(active.values())

    def dispatch(self, workflow, inputs):
        return self.request('POST', '/actions/workflows/' + workflow + '/dispatches',
                            json={'ref': 'main', 'inputs': inputs})

    def find_operation(self, operation):
        # Do not filter by branch/event: a matching name with wrong identity must fail.
        return [run for run in self.runs(operation['workflow'], created='>=' + operation['created_after'])
                if run.get('display_title') == operation['run_name']]

    def get_run(self, run_id):
        return self.request('GET', '/actions/runs/' + str(run_id))

    def download_artifact(self, artifact_id):
        import requests
        url = f'https://api.github.com/repos/{self.repository}/actions/artifacts/{artifact_id}/zip'
        with self.session.request('GET', url, timeout=30, allow_redirects=False, stream=True) as response:
            if response.status_code != 302:
                raise RuntimeError('artifact download did not return a signed redirect')
            location = response.headers.get('Location', '')
        target = urlsplit(location)
        if (target.scheme != 'https' or target.username or target.password or target.port not in (None, 443)
                or not target.hostname or not target.hostname.endswith(
                    ('.blob.core.windows.net', '.actions.githubusercontent.com'))):
            raise RuntimeError('unexpected artifact download host')
        # Signed storage URL gets no GitHub credentials and cannot redirect again.
        with requests.get(location, timeout=30, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError('artifact download failed')
            if int(response.headers.get('Content-Length', '0')) > MAX_ARCHIVE:
                raise ValueError('artifact archive is oversized')
            raw = bytearray()
            for chunk in response.iter_content(64 * 1024):
                raw.extend(chunk)
                if len(raw) > MAX_ARCHIVE:
                    raise ValueError('artifact archive is oversized')
        return bytes(raw)

    def publication_report(self, operation, expected_sha):
        if operation.get('workflow') != PUBLISH or type(operation.get('run_id')) is not int:
            raise ValueError('publication artifact requires the recorded v3 run')
        run_id = operation['run_id']

        def check(run):
            validate_run(run, operation, expected_sha)
            if run['id'] != run_id or run.get('status') != 'completed' or run.get('conclusion') != 'success':
                raise RuntimeError('publication run changed or did not succeed')
            return run

        run = check(self.get_run(run_id))
        check(self.request('GET', f'/actions/runs/{run_id}/attempts/1'))
        raw, artifact, digest = self.report_artifact(run, PUBLICATION_ARTIFACT, expected_sha)
        publication, verification = read_publication_archive(raw)
        generation = publication.get('generation')
        if publication.get('applied') is not True:
            raise RuntimeError('publication report did not apply unify')
        validate_verification(verification, generation)
        check(self.get_run(run_id))  # A rerun during download invalidates attempt 1.
        evidence = {'generation': generation, 'verification': verification,
                'artifact_id': artifact['id'], 'artifact_digest': digest,
                'run_id': run_id, 'run_attempt': 1, 'automation_id': operation['automation_id']}
        if operation.get('phase')=='web-profile':
            profile=publication.get('profile')
            if (not isinstance(profile,dict) or profile.get('extension')!=operation['extension'] or
                    any(type(profile.get(k)) is not int or profile[k]<0 for k in ('converted','identical','held')) or
                    not 1<=sum(profile[k] for k in ('converted','identical','held'))<=WEB_PROFILE_BATCH_ROWS):
                raise ValueError('invalid web profile outcome')
            evidence['profile']={k:profile[k] for k in ('converted','identical','held')}
        return evidence

    def report_artifact(self, run, name, expected_sha):
        run_id = run['id']
        inventory = self.request('GET', f'/actions/runs/{run_id}/artifacts', params={'per_page': 100})
        artifacts, total = inventory.get('artifacts'), inventory.get('total_count')
        if (not isinstance(artifacts, list) or type(total) is not int
                or total != len(artifacts) or total > 100):
            raise RuntimeError('publication artifact inventory incomplete')
        matches = [item for item in artifacts if item.get('name') == name]
        if len(matches) != 1:
            raise RuntimeError('requires one unique ' + name + ' artifact')
        artifact = matches[0]
        origin = artifact.get('workflow_run', {})
        if (type(artifact.get('id')) is not int or artifact['id'] <= 0
                or artifact.get('expired') is not False
                or type(artifact.get('size_in_bytes')) is not int
                or not 0 < artifact['size_in_bytes'] <= MAX_ARCHIVE
                or origin.get('id') != run_id or origin.get('head_sha') != expected_sha
                or origin.get('head_branch') != 'main'
                or datetime.fromisoformat(artifact['created_at'].replace('Z', '+00:00'))
                   < datetime.fromisoformat(run['run_started_at'].replace('Z', '+00:00'))):
            raise RuntimeError('foreign, replayed, expired or oversized publication artifact')
        raw = self.download_artifact(artifact['id'])
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if artifact.get('digest') != digest:
            raise RuntimeError('publication artifact digest differs or is missing')
        return raw, artifact, digest

    def continuation_report(self, previous_run_id, continuation_id, expected_sha, current_run_id):
        run = self.get_run(previous_run_id)
        validate_daily_run(run, expected_sha, completed=True)
        if run['id'] != previous_run_id:
            raise ValueError('continuation parent run ID differs')
        attempt = self.request('GET', f'/actions/runs/{previous_run_id}/attempts/1')
        validate_daily_run(attempt, expected_sha, completed=True)
        if attempt['id'] != previous_run_id:
            raise ValueError('continuation attempt run ID differs')
        raw, artifact, digest = self.report_artifact(run, f'reader-format-daily-{previous_run_id}', expected_sha)
        reports = read_report_archive(raw, {'result.json', 'operations.jsonl'})
        result = reports['result.json']
        if result != reports['operations.jsonl']:
            raise ValueError('continuation result and durable operation journal differ')
        validate_continuation(result, expected_sha, previous_run_id)
        continuation = result['continuation']
        if continuation['id'] != continuation_id:
            raise ValueError('continuation ID differs from the recorded parent intent')
        matches = self.find_operation(continuation_operation(continuation))
        if len(matches) != 1 or str(matches[0]['id']) != str(current_run_id):
            raise RuntimeError('continuation must be the unique run for its recorded intent')
        validate_run(matches[0], continuation_operation(continuation), expected_sha)
        current = self.get_run(matches[0]['id'])
        validate_daily_run(current, expected_sha)
        validate_run(current, continuation_operation(continuation), expected_sha)
        # Artifact receipts cannot authorize replay of any producer or publisher.
        for operation in result['operations']:
            observed = self.get_run(operation['run_id'])
            validate_run(observed, operation, expected_sha)
            if observed['id'] != operation['run_id'] or observed.get('conclusion') != 'success' or observed.get('status') != 'completed':
                raise RuntimeError('continuation operation is unresolved or was rerun')
        validate_daily_run(self.get_run(previous_run_id), expected_sha, completed=True)
        self.check_main(expected_sha)
        return result


def read_publication_archive(raw):
    reports = read_report_archive(raw, {'publication.json', 'format-automation-verification.json'},
                                  optional_root='v3/')
    return reports['publication.json'], reports['format-automation-verification.json']


def read_report_archive(raw, required, *, optional_root=''):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ARCHIVE:
        raise ValueError('invalid publication archive size')
    reports = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if not 1 <= len(members) <= 32 or sum(item.file_size for item in members) > MAX_EXPANDED:
            raise ValueError('publication archive expands beyond limits')
        seen = set()
        # upload-artifact chooses a common parent across upload patterns. The
        # publication artifact can be flat or rooted at the literal v3/ folder.
        # Do not strip arbitrary directories or accept mixed layouts.
        root = optional_root if optional_root and members[0].filename.startswith(optional_root) else ''
        for item in members:
            name = item.filename.removeprefix(root)
            if (item.filename != root + name or not re.fullmatch(r'[A-Za-z0-9_-]+\.jsonl?', name) or name in seen
                    or item.orig_filename != item.filename or item.is_dir() or item.flag_bits & 1
                    or (item.external_attr >> 16) & 0o170000 not in (0, 0o100000)
                    or not 0 < item.file_size <= MAX_REPORT):
                raise ValueError('unsafe, duplicate or oversized publication archive member')
            seen.add(name)
            if name in required:
                with archive.open(item) as stream:
                    payload = stream.read(MAX_REPORT + 1)
                if len(payload) != item.file_size or len(payload) > MAX_REPORT:
                    raise ValueError('publication report size differs')
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise ValueError('publication report must be an object')
                reports[name] = value
    if set(reports) != required:
        raise ValueError('publication or automation verification report missing')
    return reports


def validate_verification(receipt, generation):
    if (not isinstance(generation, str) or not GENERATION.fullmatch(generation)
            or not isinstance(receipt, dict) or receipt.get('generation') != generation
            or receipt.get('complete') is not True
            or not isinstance(receipt.get('consumers'), dict)
            or any(not isinstance(receipt['consumers'].get(surface), dict)
                   or receipt['consumers'][surface].get('generation') != generation
                   or receipt['consumers'][surface].get('projection_verified') is not True
                   for surface in ('hf', 'pages'))):
        raise RuntimeError('strict verify-generation did not confirm both consumers for the publication generation')


def validate_run(run, operation, expected_sha):
    if (run.get('display_title') != operation['run_name'] or run.get('head_sha') != expected_sha
            or run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'main'
            or type(run.get('run_attempt')) is not int or run['run_attempt'] != 1
            or run.get('path') != '.github/workflows/' + operation['workflow']
            or type(run.get('id')) is not int):
        raise RuntimeError('child run identity/attempt differs from the dispatch contract')


def validate_daily_run(run, expected_sha, *, completed=False):
    # GitHub run.name can be the evaluated run-name, not workflow.name.
    # The exact workflow path and revision remain the authoritative binding.
    event = run.get('event')
    title = run.get('display_title')
    valid_title = (isinstance(title, str) and (title == 'Reader format backlog / ' + str(event)
                   or (event == 'workflow_dispatch' and re.fullmatch(r'reader-format-continuation/[0-9a-f]{32}', title))
                   or (event == 'workflow_dispatch' and run.get('actor', {}).get('login') == 'github-actions[bot]'
                       and re.fullmatch(r'reader-search-handoff/[1-9][0-9]{0,19}', title))))
    if (not valid_title or run.get('name') not in {DAILY_NAME, title}
            or run.get('path') != '.github/workflows/' + DAILY
            or run.get('event') not in {'workflow_dispatch', 'workflow_run', 'schedule','push'}
            or run.get('head_branch') != 'main' or run.get('head_sha') != expected_sha
            or type(run.get('id')) is not int or type(run.get('run_attempt')) is not int
            or run['run_attempt'] != 1
            or (completed and (run.get('status') != 'completed' or run.get('conclusion') != 'success'))):
        raise RuntimeError('daily run identity, attempt or successful completion differs')


def continuation_operation(continuation):
    return {'workflow': DAILY, 'run_name': 'reader-format-continuation/' + continuation['id'],
            'created_after': continuation['created_after']}


def validate_continuation(result, expected_sha, previous_run_id):
    if (result.get('kind') != 'reader-format-daily' or result.get('status') != 'continuation_required'
            or result.get('expected_sha') != expected_sha
            or str(result.get('parent_run_id')) != str(previous_run_id)
            or not str(previous_run_id).isdigit() or int(previous_run_id) <= 0
            or result.get('error')):
        raise ValueError('invalid continuation result identity or status')
    continuation = result.get('continuation', {})
    if (not OPERATION_ID.fullmatch(continuation.get('id', ''))
            or continuation.get('state') != 'prepared'
            or not isinstance(continuation.get('created_after'), str)):
        raise ValueError('invalid continuation intent')
    rows = validate_summary(result['current_summary'])
    for field in ('search_generation', 'reader_generation'):
        if result.get(field) != result['current_summary'][field]:
            raise ValueError('continuation generation differs from saved summary')
    pending, triggered = result.get('remaining_formats'), result.get('triggered_formats')
    if (not isinstance(pending, list) or not pending or len(set(pending)) != len(pending)
            or not isinstance(triggered, list) or len(set(triggered)) != len(triggered)
            or not set(pending) <= set(triggered) or not set(triggered) <= set(FORMATS)):
        raise ValueError('invalid continuation format queue')
    for ext in triggered:
        decision = result['formats'][ext]
        reasons = blockers(rows[ext], source_changes_triggered=
                           decision['completion_scope'] in SOURCE_CHANGE_SCOPES)
        count='profile_outdated' if result.get('web_profile_only') is True else 'definite_backlog'
        if reasons or bool(rows[ext][count]) != (ext in pending):
            raise ValueError('continuation contains blocked or inconsistent triggered formats')
    if any(rows[ext][field] for ext in FORMATS for field in UNCERTAIN):
        raise ValueError('continuation contains pending, failure or unknown source state')
    operations = result.get('operations')
    if (not isinstance(operations, list) or not operations
            or sum(isinstance(op,dict) and op.get('phase') in {'unify','web-profile'} for op in operations)!=result.get('batches',-1)):
        raise ValueError('continuation has incomplete operation pairs')
    ids, run_ids = set(), set()
    pending_producer=None
    for operation in operations:
        identifier = operation.get('automation_id', '')
        ext = operation.get('extension'); phase=operation.get('phase')
        expected_workflow = FORMATS.get(ext, (None, None))[1] if phase=='producer' else PUBLISH
        if phase=='web-profile':
            if pending_producer is not None or FORMATS.get(ext,(None,))[0]!='web' or operation.get('inputs',{}).get('operation')!='upgrade-web-profile':
                raise ValueError('continuation has a malformed standalone web profile batch')
        elif phase=='producer' and pending_producer is None:
            pending_producer=ext
        elif phase=='unify' and pending_producer==ext:
            pending_producer=None
        else:
            raise ValueError('continuation operation pair differs')
        if (not OPERATION_ID.fullmatch(identifier) or identifier in ids
                or type(operation.get('run_id')) is not int or operation['run_id'] in run_ids
                or operation.get('run_name') != 'reader-auto/' + identifier
                or ext not in triggered or expected_workflow is None
                or operation.get('workflow') != expected_workflow
                or operation.get('expected_sha') != expected_sha
                or operation.get('status') != 'completed' or operation.get('conclusion') != 'success'):
            raise ValueError('continuation contains unresolved or duplicate operations')
        ids.add(identifier); run_ids.add(operation['run_id'])
        if phase in {'unify','web-profile'}:
            evidence = operation['publication_artifact']
            if evidence.get('run_id') != operation['run_id'] or evidence.get('run_attempt') != 1:
                raise ValueError('continuation publication receipt run differs')
            validate_verification(evidence['verification'], evidence['generation'])
            validate_verification(operation['verification'], evidence['generation'])
    if pending_producer is not None or operations[-1]['verification']['generation'] != result['reader_generation']:
        raise ValueError('continuation Reader generation differs from final publication')
    if result.get('operation_ids') != [operation['automation_id'] for operation in operations]:
        raise ValueError('continuation operation IDs differ')


def continue_dispatch(*, api, result, journal, expected_sha, current_run_id,
                      clock=time.monotonic, sleep=time.sleep, timeout_seconds=120):
    """One POST, observe only thereafter; never wait for a queued continuation to finish."""
    if str(current_run_id) != str(result.get('parent_run_id')):
        raise ValueError('continue-dispatch must run inside the originating daily run')
    validate_continuation(result, expected_sha, current_run_id)
    api.check_main(expected_sha)
    run = api.get_run(int(current_run_id))
    validate_daily_run(run, expected_sha)
    if run['id'] != int(current_run_id) or run.get('status') != 'in_progress':
        raise ValueError('continue-dispatch requires the current in-progress daily run')
    continuation = result['continuation']
    operation = continuation_operation(continuation)
    journal = Path(journal)
    posted = journal.exists()
    if posted:
        saved = json.loads(journal.read_text(encoding='utf8'))
        if saved.get('continuation_id') != continuation['id'] or saved.get('expected_sha') != expected_sha:
            raise ValueError('continuation dispatch journal identity differs')
    else:
        saved = {'continuation_id': continuation['id'], 'expected_sha': expected_sha,
                 'parent_run_id': current_run_id, 'status': 'observe_before_dispatch'}
    deadline = clock() + timeout_seconds
    while True:
        matches = api.find_operation(operation)
        if len(matches) > 1:
            raise RuntimeError('duplicate continuation run-name')
        if matches:
            validate_run(matches[0], operation, expected_sha)
            saved.update(status='observed', run_id=matches[0]['id'])
            save(journal, saved)
            return saved
        if not posted:
            if api.active_children(current_run_id):
                raise RuntimeError('active child prevents continuation dispatch')
            api.check_main(expected_sha)
            saved['status'] = 'dispatch_intent'
            save(journal, saved, exclusive=True)
            posted = True
            try:
                inputs = {'apply': 'true', 'known_writers_coordinated': 'true',
                          'continuation_run_id': str(current_run_id), 'continuation_id': continuation['id']}
                if result.get('web_profile_only') is True:
                    inputs['web_profile_only'] = 'true'
                api.dispatch(DAILY, inputs)
                saved['status'] = 'dispatched'
            except DispatchRejected as error:
                saved.update(status='dispatch_rejected', error=str(error))
                save(journal, saved)
                raise
            except Exception as error:
                saved.update(status='dispatch_unknown', error=type(error).__name__)
            save(journal, saved)
        remaining = deadline - clock()
        if remaining <= 0:
            saved['status'] = 'dispatch_unknown'
            save(journal, saved)
            return saved
        sleep(min(5, remaining))


class Controller:
    def __init__(self, *, api, refresh, verify, journal, expected_sha, enabled=False,
                 known_writers=False, parent_run_id=None, run_attempt=1, web_profile_only=False,
                 max_batches=MAX_BATCHES, timeout_seconds=MAX_SECONDS, continuation=None, boost_canary=None,
                 clock=time.monotonic, sleep=time.sleep, new_id=lambda: uuid.uuid4().hex):
        if not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha):
            raise ValueError('expected SHA must be the exact pipeline HEAD')
        if type(max_batches) is not int or not 1 <= max_batches <= MAX_BATCHES:
            raise ValueError('max_batches must be 1-20')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_SECONDS:
            raise ValueError('timeout_seconds must be 1-18000')
        if type(run_attempt) is not int or run_attempt != 1:
            raise ValueError('repeat attempt refused; observe the journal instead')
        self.api, self.refresh, self.verify = api, refresh, verify
        self.journal, self.expected_sha = Path(journal), expected_sha
        self.enabled, self.known_writers = enabled, known_writers
        self.web_profile_only = web_profile_only
        self.parent_run_id, self.max_batches = parent_run_id, max_batches
        self.clock, self.sleep, self.new_id = clock, sleep, new_id
        self.deadline = clock() + timeout_seconds
        self.finish_reserve = min(60, timeout_seconds * 0.02)
        if boost_canary is not None and (boost_canary not in boost.POLICIES or continuation is not None):
            raise ValueError('boost canary requires one supported extension and no continuation')
        self.continuation, self.boost_canary = continuation, boost_canary
        self.batch_durations = []
        self.result = None

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError('daily wall timeout; recorded operations must be observed, never redispatched')
        return remaining

    def checkpoint(self):
        save(self.journal, self.result)

    def wait(self, operation):
        while True:
            self.remaining()
            matches = self.api.find_operation(operation)
            if len(matches) > 1:
                raise RuntimeError('multiple runs match the unique automation run-name')
            if matches:
                run = matches[0]
                validate_run(run, operation, self.expected_sha)
                run = self.api.get_run(run['id'])
                validate_run(run, operation, self.expected_sha)
                if operation.get('run_id') not in (None, run['id']):
                    raise RuntimeError('operation run ID changed')
                operation.update(run_id=run['id'], status=run['status'], conclusion=run.get('conclusion'))
                self.checkpoint()
                if run['status'] == 'completed':
                    if run.get('conclusion') != 'success':
                        raise RuntimeError('child did not succeed: ' + str(run.get('conclusion')))
                    self.remaining()
                    return run
            self.sleep(min(15, self.remaining()))

    def dispatch(self, workflow, inputs, extension, phase):
        self.remaining()
        self.api.check_main(self.expected_sha)
        active = self.api.active_children(self.parent_run_id)
        if active:
            raise RuntimeError('active child/writer prevents dispatch: ' + ','.join(str(r['id']) for r in active))
        operation_id = inputs['automation_id']
        if not OPERATION_ID.fullmatch(operation_id) or operation_id in self.result['operation_ids']:
            raise ValueError('automation_id must be unique 32hex')
        operation = {'automation_id': operation_id, 'workflow': workflow, 'inputs': inputs,
                     'run_name': 'reader-auto/' + operation_id, 'extension': extension, 'phase': phase,
                     'created_after': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                     'status': 'dispatch_intent', 'expected_sha': self.expected_sha}
        self.result['operation_ids'].append(operation_id)
        self.result['operations'].append(operation)
        self.checkpoint()  # Durable intent BEFORE the only POST, including unknown outcomes.
        self.remaining()
        try:
            self.api.dispatch(workflow, inputs)
        except Exception as error:
            operation.update(status='dispatch_unknown', dispatch_error=type(error).__name__)
        else:
            operation['status'] = 'dispatched'
        self.checkpoint()
        return self.wait(operation)

    def fresh(self, baseline):
        self.remaining()
        summary = self.refresh()
        self.remaining()
        rows = validate_summary(summary)
        original = validate_summary(baseline)
        if summary['search_generation'] != baseline['search_generation']:
            raise RuntimeError('search generation changed')
        if summary.get('source_revisions') != baseline.get('source_revisions'):
            raise RuntimeError('source revisions changed')
        if set(rows) != set(original) or any(rows[ext].get('sources') != original[ext].get('sources') for ext in rows):
            raise RuntimeError('source inventory changed')
        return summary

    def update_counts(self, summary, triggered):
        latest = plan(summary, boost_canary=self.boost_canary)
        for ext, decision in latest['formats'].items():
            if ext not in triggered:
                if self.web_profile_only and (FORMATS.get(ext,(None,))[0]!='web' or not decision.get('counts',{}).get('profile_outdated')):
                    self.result['blocked'].pop(ext,None)
                    if decision['state'] in {'triggered','blocked'}:
                        decision=dict(decision,state='deferred',reasons=['outside this web profile bump'])
                    self.result['formats'][ext]=decision
                    continue
                self.result['formats'][ext] = decision
                if decision['state'] == 'blocked':
                    self.result['blocked'][ext] = decision['reasons']
                else:
                    self.result['blocked'].pop(ext, None)
            else:
                self.result['formats'][ext]['counts'] = decision['counts']
                for field in ('conversion_dependencies_checked', 'dependency_freshness', 'warnings'):
                    self.result['formats'][ext][field] = decision[field]
        self.result['reader_generation'] = summary['reader_generation']

    def prepare_continuation(self, current, queue):
        self.result.update(status='continuation_required', remaining_formats=list(queue),
                           current_summary=current,
                           continuation={'id': self.new_id(), 'state': 'prepared',
                                         'created_after': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
        for ext in queue:
            self.result['formats'][ext]['state'] = 'continuation_required'
        validate_continuation(self.result, self.expected_sha, self.parent_run_id)
        self.checkpoint()

    def run(self, summary):
        self.result = plan(summary, boost_canary=self.boost_canary)
        self.result.update(mode='run', status='running', expected_sha=self.expected_sha,
                           parent_run_id=self.parent_run_id)
        if self.web_profile_only:
            self.result['web_profile_only']=True
        if self.boost_canary is not None:
            self.result['boost_canary'] = self.boost_canary
        # Refuse reuse even when the previous controller crashed before its first POST.
        save(self.journal, self.result, exclusive=True)
        try:
            if self.enabled is not True or self.known_writers is not True:
                raise RuntimeError('workflow enabled and known-writers gates are required')
            self.api.check_main(self.expected_sha)
            active = self.api.active_children(self.parent_run_id)
            if active:
                raise RuntimeError('active child/writer: ' + ','.join(str(r['id']) for r in active))
            current = self.fresh(summary)
            if current['reader_generation'] != summary['reader_generation']:
                raise RuntimeError('Reader generation changed before first dispatch')
            triggered = [ext for ext, item in self.result['formats'].items() if item['state'] == 'triggered']
            queue = sorted(triggered, key=lambda ext: (ext != 'epub', FORMATS[ext][0] == 'text', ext))
            if self.web_profile_only:
                # A producer profile bump must not accidentally trigger
                # unrelated format migrations from a routine push.
                rows=validate_summary(current)
                triggered=[ext for ext in triggered if FORMATS[ext][0]=='web' and rows[ext]['profile_outdated']]
                queue=[ext for ext in queue if ext in triggered]
            if self.continuation is not None:
                previous = self.continuation
                validate_continuation(previous, self.expected_sha, previous['parent_run_id'])
                self.web_profile_only=previous.get('web_profile_only') is True
                if self.web_profile_only:
                    self.result['web_profile_only']=True
                for field in ('search_generation', 'reader_generation'):
                    if current[field] != previous[field]:
                        raise RuntimeError('continuation ' + field + ' changed')
                baseline = validate_summary(previous['current_summary'])
                rows = validate_summary(current)
                compared = (ext for ext in rows if not self.web_profile_only or FORMATS[ext][0] == 'web')
                if any(rows[ext] != baseline[ext] for ext in compared):
                    raise RuntimeError('continuation source inventory or counts changed')
                triggered = previous['triggered_formats']
                queue = list(previous['remaining_formats'])
                for ext in triggered:
                    self.result['formats'][ext]['completion_scope'] = previous['formats'][ext]['completion_scope']
                    self.result['formats'][ext]['state'] = 'triggered' if ext in queue else 'complete'
                    self.result['blocked'].pop(ext, None)
                self.result['continued_from_run_id'] = previous['parent_run_id']
                self.result['accepted_continuation_id'] = previous['continuation']['id']
            self.result['triggered_formats'] = triggered
            self.update_counts(current, triggered)
            if self.boost_canary is not None and not queue:
                raise RuntimeError('boost canary requires an initially triggered ordinary backlog')
            while queue:
                # Yield only between fully published, refreshed batches. A timeout
                # with a running child or unknown POST remains blocked for observation.
                reserve = max(self.batch_durations, default=0) * 1.5 + self.finish_reserve
                if self.result['batches'] and (self.result['batches'] >= self.max_batches
                                               or self.remaining() <= reserve):
                    self.prepare_continuation(current, queue)
                    return self.result
                ext = queue.pop(0)
                row = validate_summary(current)[ext]
                decision = self.result['formats'][ext]
                decision['counts'] = {key: row[key] for key in COUNTS}
                reasons = blockers(row, source_changes_triggered=
                                   decision['completion_scope'] in SOURCE_CHANGE_SCOPES)
                if reasons:
                    decision.update(state='blocked', reasons=reasons)
                    self.result['blocked'][ext] = reasons
                    continue
                boost_limit = None
                if self.boost_canary is not None:
                    boost_limit = boost.batch_limit(ext, row['not_mapped'] + row['source_content_changed'])
                    if boost_limit <= boost.STANDARD_LIMIT:
                        raise RuntimeError('refreshed backlog is too small for a boosted batch')
                if row['definite_backlog'] == 0:
                    decision['state'] = 'complete'
                    continue
                if self.web_profile_only and not row['profile_outdated']:
                    decision['state']='complete'
                    continue
                started = self.clock()
                self.result['batches'] += 1
                if FORMATS[ext][0]=='web' and row['profile_outdated']:
                    if self.boost_canary is not None:
                        raise RuntimeError('ordinary canary cannot publish profile work')
                    self.dispatch(PUBLISH, {'operation':'upgrade-web-profile','apply':'true','automation_id':self.new_id(),
                                            'resource_json':json.dumps({'extension':ext,'search_generation':summary['search_generation'],
                                                                        'parent_run_id':int(self.parent_run_id)},separators=(',',':'))}, ext, 'web-profile')
                else:
                    producer_id = self.new_id()
                    self.dispatch(FORMATS[ext][1], producer_inputs(
                        ext, producer_id, expected_sha=self.expected_sha,
                        search_generation=summary['search_generation'], boost_limit=boost_limit), ext, 'producer')
                    self.dispatch(PUBLISH, {'operation': 'unify', 'apply': 'true',
                                            'automation_id': self.new_id()}, ext, 'unify')
                operation = self.result['operations'][-1]
                evidence = self.api.publication_report(operation, self.expected_sha)
                generation = evidence['generation']
                validate_verification(evidence['verification'], generation)
                operation['publication_artifact'] = evidence
                self.checkpoint()
                receipt = self.verify(generation, self.remaining())
                self.remaining()
                validate_verification(receipt, generation)
                current = self.fresh(summary)
                if current['reader_generation'] != generation:
                    raise RuntimeError('Reader generation changed after verification')
                after = validate_summary(current)[ext]
                if operation['phase']=='web-profile':
                    profile=evidence['profile']
                    if (row['profile_outdated']-after['profile_outdated']!=sum(profile.values())
                            or after['profile_upgrade_deferred']-row['profile_upgrade_deferred']!=profile['held']):
                        raise RuntimeError('web profile counts differ from the exact central outcome')
                    aggregate=decision.setdefault('web_profile',{'converted':0,'identical':0,'held':0})
                    for k,v in profile.items():aggregate[k]+=v
                decision['counts'] = {key: after[key] for key in COUNTS}
                self.update_counts(current, triggered)
                operation['verification'] = receipt
                self.checkpoint()
                if after['definite_backlog'] >= row['definite_backlog']:
                    raise RuntimeError('zero progress: conversion backlog did not decrease for ' + ext)
                self.batch_durations.append(self.clock() - started)
                reasons = blockers(after, source_changes_triggered=
                                   decision['completion_scope'] in SOURCE_CHANGE_SCOPES)
                if reasons:
                    decision.update(state='blocked', reasons=reasons)
                    self.result['blocked'][ext] = reasons
                elif self.boost_canary is not None:
                    decision['state'] = 'canary_remaining' if after['definite_backlog'] else 'complete'
                    self.result['status'] = 'canary_complete'
                    return self.result  # Verified decrease; deliberately do not enqueue more work.
                elif (after['profile_outdated'] if self.web_profile_only else after['definite_backlog']):
                    queue.append(ext)  # Round robin until the selected backlog reaches zero.
                else:
                    decision['state'] = 'complete'
            # Recheck previously drained formats too: a later batch cannot hide
            # newly observed failures/pending checkpoints elsewhere in the inventory.
            final_rows = validate_summary(current)
            for ext in triggered:
                reasons = blockers(final_rows[ext], source_changes_triggered=
                                   self.result['formats'][ext]['completion_scope'] in SOURCE_CHANGE_SCOPES)
                if final_rows[ext]['profile_outdated' if self.web_profile_only else 'definite_backlog']:
                    reasons.append('triggered format still has definite backlog')
                if reasons:
                    self.result['formats'][ext].update(state='blocked', reasons=reasons)
                    self.result['blocked'][ext] = reasons
            self.result['status'] = 'blocked' if self.result['blocked'] else 'complete'
        except (Exception, WallTimeout) as error:
            self.result.update(status='blocked', error=type(error).__name__ + ': ' + str(error))
            self.result['blocked']['controller'] = [self.result['error']]
        finally:
            for ext, decision in self.result['formats'].items():
                if (decision['state'] == 'triggered' or
                        (decision['state'] == 'continuation_required' and self.result['status'] != 'continuation_required')):
                    decision.update(state='blocked', reasons=['interrupted with backlog remaining'])
                    self.result['blocked'][ext] = decision['reasons']
            if self.result['blocked'] and self.result['status'] != 'continuation_required':
                self.result['status'] = 'blocked'
            self.checkpoint()
        return self.result

    def observe(self):
        """Read-only recovery: never executes a producer, unify, or another batch."""
        self.result = json.loads(self.journal.read_text(encoding='utf8'))
        if (self.result.get('kind') != 'reader-format-daily' or self.result.get('expected_sha') != self.expected_sha
                or not isinstance(self.result.get('operations'), list)):
            raise ValueError('journal identity differs')
        try:
            for operation in self.result['operations']:
                identifier = operation.get('automation_id', '')
                if (not OPERATION_ID.fullmatch(identifier)
                        or operation.get('run_name') != 'reader-auto/' + identifier
                        or operation.get('workflow') not in {PUBLISH, *(v[1] for v in FORMATS.values() if v[1])}):
                    raise ValueError('invalid journal operation')
                self.wait(operation)
            self.result['recovery'] = 'observed; review publication and inspect before any new daily run'
        except (Exception, WallTimeout) as error:
            self.result['recovery'] = type(error).__name__ + ': ' + str(error)
        self.result['status'] = 'blocked'
        self.result.setdefault('blocked', {})['recovery'] = ['observation only; no automatic cross-run replay']
        self.checkpoint()
        return self.result


def live_callbacks(*, web_profile_only=False):
    # Keep plan/observation and unit tests independent of Hub conversion dependencies.
    import httpx
    from huggingface_hub import HfApi, HfFileSystem
    try:
        from . import reader_format_backlog as backlog, publish_reader_v3 as publication
    except ImportError:
        import reader_format_backlog as backlog
        import publish_reader_v3 as publication
    token = os.environ.get('HF_TOKEN')
    hub, api, fs = backlog.HubBucketStore(), HfApi(token=token), HfFileSystem(token=token)

    def refresh():
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            return backlog.inspect(hub=hub, api=api, fs=fs, client=client, threshold=THRESHOLD,
                                   scope='web' if web_profile_only else 'all',
                                   include_profile_upgrades=os.getenv('READER_CHM_PROFILE_REBUILD_ENABLED') == 'true',
                                   include_web_profile_upgrades=os.getenv('READER_WEB_PROFILE_REBUILD_ENABLED') == 'true')[0]

    def verify(generation, remaining):
        with httpx.Client(timeout=min(30, remaining), follow_redirects=True) as client:
            return publication.verify_generation(hub, client, generation,
                                                 timeout_seconds=min(1200, int(remaining)))
    return refresh, verify


def guard_source(*, generation, expected_sha, revisions, hub, api, github,
                 capture=None, head=None):
    """Read-only source pin check, repeated by producers immediately before writes.

    This is point-in-time validation, not a source-head lease or an eligible-key
    filter. The converter must still consume the fetched immutable revisions.
    """
    if (not isinstance(generation, str) or not GENERATION.fullmatch(generation)
            or not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha)):
        raise ValueError('invalid source guard generation or expected SHA')
    if (head or local_head)() != expected_sha:
        raise ValueError('source guard checkout differs from expected pipeline SHA')
    github.check_main(expected_sha)
    if (not isinstance(revisions, dict) or not revisions
            or any(not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+', repo)
                   or not isinstance(revision, str) or not SHA.fullmatch(revision)
                   for repo, revision in revisions.items())):
        raise ValueError('state/commits.json must be a repository to immutable revision mapping')
    try:
        from . import reader_format_backlog as backlog
    except ImportError:
        import reader_format_backlog as backlog
    pointer_raw = hub.read_bytes(backlog.SEARCH_BUCKET, backlog.CURRENT)
    if backlog.search_publication_pointer.decode_pointer(pointer_raw)['desired'] != generation:
        raise ValueError('published search generation differs from producer pin')
    captured_pointer, manifest_raw, _corpus, pinned_revisions, _observed = (capture or backlog.capture_search)(hub)
    if captured_pointer != pointer_raw:
        raise ValueError('published search pointer changed during source capture')
    if revisions != pinned_revisions:
        raise ValueError('fetched state/commits.json differs from published source revisions')
    for repo, expected in sorted(revisions.items()):
        info = backlog.retry_read(lambda: api.repo_info(repo, repo_type='dataset', timeout=30))
        if getattr(info, 'sha', None) != expected:
            raise ValueError('current source head differs from published revision: ' + repo)
    if (hub.read_bytes(backlog.SEARCH_BUCKET, backlog.CURRENT) != pointer_raw
            or hub.read_bytes(backlog.SEARCH_BUCKET, f'generations/{generation}/manifest.json') != manifest_raw):
        raise ValueError('published search pointer or manifest changed during source guard')
    github.check_main(expected_sha)
    if (head or local_head)() != expected_sha:
        raise ValueError('pipeline checkout changed during source guard')
    return {'version': 1, 'kind': 'reader-format-source-guard', 'search_generation': generation,
            'expected_sha': expected_sha, 'source_revisions': revisions,
            'current_source_heads_checked': True, 'remote_writes': 0}


def local_head():
    root = Path(__file__).resolve().parents[1]
    return subprocess.check_output(['git', '-c', 'safe.directory=' + str(root), 'rev-parse', 'HEAD'],
                                   cwd=root, text=True).strip()


@contextmanager
def wall_timeout(seconds):
    """CLI runs on Linux's main thread; interrupt even a blocking Hub refresh."""
    def expired(_signum, _frame):
        raise WallTimeout('daily wall timeout; observe the durable journal before recovery')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'run', 'observe', 'guard-source', 'continue-dispatch'))
    parser.add_argument('--result', type=Path)
    parser.add_argument('--continuation-run-id', type=int)
    parser.add_argument('--continuation-id')
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--boost-canary', choices=tuple(boost.POLICIES))
    parser.add_argument('--generation')
    parser.add_argument('--revisions', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--journal', type=Path)
    parser.add_argument('--expected-sha')
    parser.add_argument('--enabled', action='store_true')
    parser.add_argument('--known-writers-verified', action='store_true')
    parser.add_argument('--max-batches', type=int, default=MAX_BATCHES)
    parser.add_argument('--timeout-seconds', type=int, default=MAX_SECONDS)
    args = parser.parse_args(argv)
    if args.boost_canary and (args.command != 'run' or args.continuation_run_id is not None
                             or args.continuation_id is not None or not args.enabled
                             or not args.known_writers_verified
                             or os.getenv('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                             or os.getenv('GITHUB_RUN_ATTEMPT') != '1'
                             or os.getenv(boost.POLICIES[args.boost_canary][2]) != 'true'):
        parser.error('boost canary requires an enabled manual first-attempt controller without continuation')
    if args.command == 'guard-source':
        if not args.generation or not args.expected_sha or args.revisions is None:
            parser.error('guard-source requires --generation, --expected-sha and --revisions')
        if args.output is not None:
            parser.error('guard-source writes only its JSON receipt to stdout')
        from huggingface_hub import HfApi
        try:
            from .reader_bucket_store import HubBucketStore
        except ImportError:
            from reader_bucket_store import HubBucketStore
        api = GitHub(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'), local_head=local_head)
        result = guard_source(generation=args.generation, expected_sha=args.expected_sha,
                              revisions=json.loads(args.revisions.read_text(encoding='utf8')),
                              hub=HubBucketStore(), api=HfApi(token=os.environ.get('HF_TOKEN')), github=api)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.output is None:
        parser.error('--output is required')
    paths = [path.resolve() for path in (args.summary, args.output, args.journal, args.result) if path is not None]
    if len(set(paths)) != len(paths) or args.output.exists() or args.output.is_symlink():
        parser.error('summary, journal and new output must be distinct paths')
    if args.command == 'continue-dispatch':
        if (args.result is None or args.journal is None or args.expected_sha is None
                or not args.enabled or not args.known_writers_verified
                or os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1'
                or not os.environ.get('GITHUB_RUN_ID') or local_head() != args.expected_sha):
            parser.error('continue-dispatch requires current first-attempt run, result, journal, exact SHA and gates')
        api = GitHub(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'), local_head=local_head)
        result = continue_dispatch(api=api, result=json.loads(args.result.read_text(encoding='utf8')),
                                   journal=args.journal, expected_sha=args.expected_sha,
                                   current_run_id=os.environ['GITHUB_RUN_ID'])
        save(args.output, result, exclusive=True)
        print(json.dumps(result, sort_keys=True))
        # Unknown dispatch must leave the parent successful so an accepted queued
        # continuation can validate it. Never retry a POST on an unknown response.
        return 0
    if args.command != 'observe' and args.summary is None:
        parser.error('--summary is required (the parent must inspect first)')
    if bool(args.continuation_run_id) != bool(args.continuation_id):
        parser.error('both continuation inputs are required')
    if args.continuation_id and (args.command != 'run' or not OPERATION_ID.fullmatch(args.continuation_id)):
        parser.error('continuation inputs require run and a 32hex ID')
    if args.command == 'plan':
        result = plan(json.loads(args.summary.read_text(encoding='utf8')))
    else:
        if args.journal is None or args.expected_sha is None:
            parser.error('--journal and --expected-sha are required')
        if local_head() != args.expected_sha:
            parser.error('--expected-sha must equal local pipeline HEAD')
        api = GitHub(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'), local_head=local_head)
        continuation = None
        if args.continuation_run_id:
            continuation = api.continuation_report(args.continuation_run_id, args.continuation_id,
                                                   args.expected_sha, os.environ.get('GITHUB_RUN_ID'))
        web_profile_only = (continuation.get('web_profile_only') is True if continuation is not None else
                            os.getenv('GITHUB_EVENT_NAME') == 'push' or os.getenv('WEB_PROFILE_ONLY') == 'true')
        refresh, verify = live_callbacks(web_profile_only=web_profile_only) if args.command == 'run' else (None, None)
        controller = Controller(api=api, refresh=refresh, verify=verify, journal=args.journal,
                                expected_sha=args.expected_sha, enabled=args.enabled,
                                known_writers=args.known_writers_verified,
                                web_profile_only=web_profile_only,
                                parent_run_id=os.environ.get('GITHUB_RUN_ID'),
                                run_attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
                                max_batches=args.max_batches, timeout_seconds=args.timeout_seconds,
                                continuation=continuation, boost_canary=args.boost_canary)
        with wall_timeout(args.timeout_seconds):
            result = (controller.observe() if args.command == 'observe' else
                      controller.run(json.loads(args.summary.read_text(encoding='utf8'))))
    save(args.output, result, exclusive=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
