"""Bounded Reader format controller; workflow gates authorize known writers.

The parent must use concurrency group reader-format-daily (never reader-sidecar
or daily-sync), inspect first, and upload --journal and --output with always().
Producers accept automation_id, defer_unify=true and run-name
reader-auto/{automation_id}; media additionally accepts extension. Publication
accepts automation_id with the same run-name, but no defer_unify input.

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


REPOSITORY = 'anftm/pipeline'
PUBLISH = 'reader-v3-publish.yml'
THRESHOLD = 10
MAX_BATCHES = 20
MAX_SECONDS = 5 * 60 * 60
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
    'text': ('txt md markdown vcf ini', None, 0),
}
FORMATS = {ext: (family, workflow, limit) for family, (exts, workflow, limit) in FAMILIES.items()
           for ext in exts.split()}
COUNTS = ('sources', 'not_mapped', 'source_content_changed', 'source_content_unchanged',
          'source_identity_unknown', 'mapped_content_not_checked', 'recorded_failure_current',
          'pending_shard_checkpoint', 'definite_backlog')
UNCERTAIN = ('source_identity_unknown', 'mapped_content_not_checked',
             'recorded_failure_current', 'pending_shard_checkpoint')


def validate_summary(summary):
    if (not isinstance(summary, dict) or summary.get('version') != 1
            or summary.get('kind') != 'reader-format-backlog'
            or type(summary.get('threshold')) is not int or summary['threshold'] != THRESHOLD):
        raise ValueError('requires a threshold-10 reader-format-backlog summary')
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
        if (row['definite_backlog'] != row['not_mapped'] + row['source_content_changed']
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


def blockers(row):
    reasons = [field for field in UNCERTAIN if row[field]]
    if row['all_source_identities_checked'] is not True:
        reasons.append('all_source_identities_checked is not true')
    if row['conversion_dependencies_checked'] is not True:
        reasons.append('conversion_dependencies_checked is not true')
    if FORMATS[row['extension']][1] is None:
        reasons.append('no automatic workflow')
    return reasons


def plan(summary):
    rows = validate_summary(summary)
    decisions = {}
    for ext, row in sorted(rows.items()):
        if ext in {'pdf', 'djvu'}:
            decisions[ext] = {'state': 'excluded', 'reasons': ['existing dataset-primary policy']}
            continue
        reasons = blockers(row)
        state = ('blocked' if reasons else 'triggered' if row['definite_backlog'] >= THRESHOLD
                 else 'complete' if row['definite_backlog'] == 0 else 'below_threshold')
        decisions[ext] = {'state': state, 'reasons': reasons,
                          'counts': {key: row[key] for key in COUNTS}, 'workflow': FORMATS[ext][1]}
    return {'version': 1, 'kind': 'reader-format-daily', 'mode': 'plan',
            'search_generation': summary['search_generation'], 'reader_generation': summary['reader_generation'],
            'formats': decisions, 'blocked': {ext: row['reasons'] for ext, row in decisions.items()
                                              if row['state'] == 'blocked'},
            'batches': 0, 'operation_ids': [], 'operations': [], 'status': 'planned'}


def producer_inputs(extension, automation_id, *, expected_sha, search_generation):
    family, _workflow, limit = FORMATS[extension]
    if (not _workflow or not OPERATION_ID.fullmatch(automation_id)
            or not SHA.fullmatch(expected_sha) or not GENERATION.fullmatch(search_generation)):
        raise ValueError('invalid producer operation')
    inputs = {'limit': str(limit), 'apply': 'true', 'automation_id': automation_id, 'defer_unify': 'true',
              'expected_sha': expected_sha, 'search_generation': search_generation}
    if family == 'chm':
        inputs.update(retry_failed='false', failed_only='false')
    else:
        inputs.update(extension=extension, retry_failures='false')
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
        response = self.session.request(method, 'https://api.github.com/repos/' + self.repository + path,
                                        timeout=30, allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            raise RuntimeError('GitHub ' + method + ' returned ' + str(response.status_code))
        return response.json() if response.status_code != 204 else None

    def check_main(self, expected_sha):
        if self.local_head and self.local_head() != expected_sha:
            raise RuntimeError('local pipeline HEAD changed')
        if self.request('GET', '/git/ref/heads/main')['object']['sha'] != expected_sha:
            raise RuntimeError('pipeline main differs from expected HEAD')

    def runs(self, workflow=None, **params):
        path = '/actions/' + ('workflows/' + workflow + '/' if workflow else '') + 'runs'
        result = {}
        for page in range(1, 11):
            data = self.request('GET', path, params={**params, 'per_page': 100, 'page': page})
            rows, total = data.get('workflow_runs'), data.get('total_count')
            if not isinstance(rows, list) or type(total) is not int or total > 1000:
                raise RuntimeError('run inventory incomplete or exceeds observation bound')
            for row in rows:
                result[row['id']] = row
            if page * 100 >= total:
                if len(result) != total:
                    raise RuntimeError('run inventory changed or is incomplete')
                return list(result.values())
        raise RuntimeError('run inventory pagination incomplete')

    def active_children(self, parent_run_id=None):
        active = {}
        for status in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
            for row in self.runs(status=status):
                name = row.get('path', '').split('/')[-1].split('@')[0]
                if (str(row.get('id')) != str(parent_run_id)
                        and (name.startswith(('reader-', 'search-daily-', 'search-promotion-'))
                             or name == 'pdf-account-worker.yml')
                        and row.get('status') != 'completed'):
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
        inventory = self.request('GET', f'/actions/runs/{run_id}/artifacts', params={'per_page': 100})
        artifacts, total = inventory.get('artifacts'), inventory.get('total_count')
        if (not isinstance(artifacts, list) or type(total) is not int
                or total != len(artifacts) or total > 100):
            raise RuntimeError('publication artifact inventory incomplete')
        matches = [item for item in artifacts if item.get('name') == PUBLICATION_ARTIFACT]
        if len(matches) != 1:
            raise RuntimeError('requires one unique v3-publication-report artifact')
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
        publication, verification = read_publication_archive(raw)
        generation = publication.get('generation')
        if publication.get('applied') is not True:
            raise RuntimeError('publication report did not apply unify')
        validate_verification(verification, generation)
        check(self.get_run(run_id))  # A rerun during download invalidates attempt 1.
        return {'generation': generation, 'verification': verification,
                'artifact_id': artifact['id'], 'artifact_digest': digest,
                'run_id': run_id, 'run_attempt': 1, 'automation_id': operation['automation_id']}


def read_publication_archive(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ARCHIVE:
        raise ValueError('invalid publication archive size')
    reports = {}
    required = {'publication.json', 'format-automation-verification.json'}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if not 1 <= len(members) <= 32 or sum(item.file_size for item in members) > MAX_EXPANDED:
            raise ValueError('publication archive expands beyond limits')
        seen = set()
        for item in members:
            name = item.filename
            # upload-artifact uploads output/v3/*.json with a flat archive root.
            if (not re.fullmatch(r'[A-Za-z0-9_-]+\.json', name) or name in seen
                    or item.orig_filename != name or item.is_dir() or item.flag_bits & 1
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
    return reports['publication.json'], reports['format-automation-verification.json']


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


class Controller:
    def __init__(self, *, api, refresh, verify, journal, expected_sha, enabled=False,
                 known_writers=False, parent_run_id=None, run_attempt=1,
                 max_batches=MAX_BATCHES, timeout_seconds=MAX_SECONDS,
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
        self.parent_run_id, self.max_batches = parent_run_id, max_batches
        self.clock, self.sleep, self.new_id = clock, sleep, new_id
        self.deadline = clock() + timeout_seconds
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
        latest = plan(summary)
        for ext, decision in latest['formats'].items():
            if ext not in triggered:
                self.result['formats'][ext] = decision
                if decision['state'] == 'blocked':
                    self.result['blocked'][ext] = decision['reasons']
                else:
                    self.result['blocked'].pop(ext, None)
            else:
                self.result['formats'][ext]['counts'] = decision['counts']
        self.result['reader_generation'] = summary['reader_generation']

    def run(self, summary):
        self.result = plan(summary)
        self.result.update(mode='run', status='running', expected_sha=self.expected_sha,
                           parent_run_id=self.parent_run_id)
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
            self.update_counts(current, triggered)
            for ext in triggered:
                while True:
                    row = validate_summary(current)[ext]
                    decision = self.result['formats'][ext]
                    decision['counts'] = {key: row[key] for key in COUNTS}
                    reasons = blockers(row)
                    if reasons:
                        decision.update(state='blocked', reasons=reasons)
                        self.result['blocked'][ext] = reasons
                        break
                    if row['definite_backlog'] == 0:
                        decision['state'] = 'complete'
                        break
                    if self.result['batches'] >= self.max_batches:
                        raise RuntimeError('batch budget exhausted with definite backlog remaining')
                    self.result['batches'] += 1
                    producer_id = self.new_id()
                    self.dispatch(FORMATS[ext][1], producer_inputs(
                        ext, producer_id, expected_sha=self.expected_sha,
                        search_generation=summary['search_generation']), ext, 'producer')
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
                    decision['counts'] = {key: after[key] for key in COUNTS}
                    self.update_counts(current, triggered)
                    self.result['operations'][-1]['verification'] = receipt
                    self.checkpoint()
                    if after['definite_backlog'] >= row['definite_backlog']:
                        raise RuntimeError('zero progress: source backlog did not decrease for ' + ext)
                    # Once triggered, continue below ten until zero, subject to all guards.
            # Recheck previously drained formats too: a later batch cannot hide
            # newly observed failures/pending checkpoints elsewhere in the inventory.
            final_rows = validate_summary(current)
            for ext in triggered:
                reasons = blockers(final_rows[ext])
                if final_rows[ext]['definite_backlog']:
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
                if decision['state'] == 'triggered':
                    decision.update(state='blocked', reasons=['interrupted with backlog remaining'])
                    self.result['blocked'][ext] = decision['reasons']
            if self.result['blocked']:
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


def live_callbacks():
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
            return backlog.inspect(hub=hub, api=api, fs=fs, client=client, threshold=10, scope='all')[0]

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
    parser.add_argument('command', choices=('plan', 'run', 'observe', 'guard-source'))
    parser.add_argument('--summary', type=Path)
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
    paths = [path.resolve() for path in (args.summary, args.output, args.journal) if path is not None]
    if len(set(paths)) != len(paths) or args.output.exists() or args.output.is_symlink():
        parser.error('summary, journal and new output must be distinct paths')
    if args.command != 'observe' and args.summary is None:
        parser.error('--summary is required (the parent must inspect first)')
    if args.command == 'plan':
        result = plan(json.loads(args.summary.read_text(encoding='utf8')))
    else:
        if args.journal is None or args.expected_sha is None:
            parser.error('--journal and --expected-sha are required')
        if local_head() != args.expected_sha:
            parser.error('--expected-sha must equal local pipeline HEAD')
        api = GitHub(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'), local_head=local_head)
        refresh, verify = live_callbacks() if args.command == 'run' else (None, None)
        controller = Controller(api=api, refresh=refresh, verify=verify, journal=args.journal,
                                expected_sha=args.expected_sha, enabled=args.enabled,
                                known_writers=args.known_writers_verified,
                                parent_run_id=os.environ.get('GITHUB_RUN_ID'),
                                run_attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
                                max_batches=args.max_batches, timeout_seconds=args.timeout_seconds)
        with wall_timeout(args.timeout_seconds):
            result = (controller.observe() if args.command == 'observe' else
                      controller.run(json.loads(args.summary.read_text(encoding='utf8'))))
    save(args.output, result, exclusive=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
