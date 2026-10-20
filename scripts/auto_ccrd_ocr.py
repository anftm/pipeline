#!/usr/bin/env python3
"""Admit complete OCR without proofreading; reuse the sole central CCRD writer."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re

try:
    from . import ccrd_ocr_release as contract, ccrd_lifecycle as lifecycle
    from . import export_reader_txt as exporter, build_ccrd_ocr_release as builder
    from . import publish_ccrd_ocr as publisher, publish_reader_v3 as reading, shared
except ImportError:
    import ccrd_ocr_release as contract
    import ccrd_lifecycle as lifecycle
    import export_reader_txt as exporter
    import build_ccrd_ocr_release as builder
    import publish_ccrd_ocr as publisher
    import publish_reader_v3 as reading
    import shared


REGISTRY = 'reader-index/pdf_ocr_manifest.json'
DERIVED = 'reader-index/derived_pdf_manifest.json'
STATE = 'control/ocr-automation.json'


class StaleRetainedProvenance(ValueError):
    """The export references a Reader resource whose exact bytes changed."""


def require_retained(store, value):
    """Reject one stale export without blocking every other complete book."""
    for ref in value.get('retained_reader_resources', []):
        try:
            raw = store.read_bytes(ref['bucket'], ref['path'])
        except FileNotFoundError as error:
            raise StaleRetainedProvenance(
                f"retained resource is missing: {ref['bucket']}/{ref['path']}") from error
        if len(raw) != ref['bytes'] or contract.digest(raw) != ref['sha256']:
            raise StaleRetainedProvenance(
                f"retained resource differs: {ref['bucket']}/{ref['path']}")


def configuration(value):
    if (value.get('version') != 1
            or not re.fullmatch(r'[0-9a-f]{40}', str(value.get('consumer_revision', '')))):
        raise ValueError('OCR automation requires an approved immutable consumer')
    contract.sha(value.get('builder_sha256')); contract.sha(value.get('base_metadata_sha256'))
    return value


def store_local(root, ref, raw):
    lifecycle.resource(ref)
    if ref['bucket'] != contract.BUCKET or contract.digest(raw) != ref['sha256'] or len(raw) != ref['bytes']:
        raise ValueError('OCR local materialization checksum/bucket mismatch')
    target = root / contract.safe_path(ref['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != raw:
        raise ValueError('conflicting OCR candidate object')
    target.write_bytes(raw)


def current_exports(store, root):
    pointer, _raw = publisher.optional_json(store, publisher.CURRENT)
    refs, exports = {}, {}
    if pointer:
        ref = pointer['release']
        raw = publisher.verified_remote(store, ref)
        manifest = json.loads(raw)
        lifecycle.references(manifest, ref['path'])
        if manifest['generation'] != pointer['generation']:
            raise ValueError('OCR pointer/release generation mismatch')
        store_local(root, ref, raw)
        for ref in manifest['exports']:
            raw = publisher.verified_remote(store, ref)
            value = json.loads(raw)
            text_ref = value['text']
            store_local(root, ref, raw)
            target = root / contract.safe_path(text_ref['path'])
            if not target.exists():
                store_local(root, text_ref, publisher.verified_remote(store, text_ref))
            _record, value = contract.export_record(root, ref)
            book = value['book_id']
            if book in exports:
                raise ValueError('duplicate live OCR book identity')
            exports[book], refs[book] = value, ref
    return pointer, refs, exports


def discover(registry, derived, catalog, exports, state, *, retry_failures=False):
    if registry.get('version') != 1 or not isinstance(registry.get('files'), dict):
        raise ValueError('invalid complete-OCR registry')
    if (derived.get('version') != 1 or derived.get('kind') != 'pdf-derived-source-index'
            or not isinstance(derived.get('files'), list)):
        raise ValueError('invalid derived source registry')
    derivations = {item['key']: item for item in derived['files']}
    if len(derivations) != len(derived['files']):
        raise ValueError('duplicate derived source identity')
    jobs, deferred, quarantined = [], [], []
    for key, entry in sorted(registry['files'].items()):
        if entry.get('status') != 'ready':
            continue
        book = contract.digest(key.encode())
        active, old = catalog['files'].get(key), exports.get(book)
        derived_entry = derivations.get(key) if entry.get('source_kind', 'upstream') != 'upstream' else None
        identity = {'key': key, 'entry': entry, 'active': active, 'derivation': derived_entry}
        fingerprint = contract.digest(contract.canonical(identity))
        # Quarantine identity is the immutable input, not the mutable live
        # release: a catalog/export change elsewhere must not retry a source
        # whose exact bytes already failed. A changed source, text layer or
        # derivation still produces a new fingerprint and is reconsidered.
        source_identity = {
            'key': key,
            'source_sha256': entry.get('source_sha256'),
            'source_revision': entry.get('source_revision'),
            'page_count': entry.get('page_count'),
            'text_layer': entry.get('text_layer'),
            'derivation': None if not derived_entry else {
                'source_sha256': derived_entry.get('source_sha256'),
                'derived_sha256': derived_entry.get('derived_sha256'),
                'derived_bytes': derived_entry.get('derived_bytes'),
                'manifest_path': derived_entry.get('manifest_path'),
            },
        }
        quarantine = contract.digest(contract.canonical(source_identity))
        if not retry_failures and state['failures'].get(key, {}).get('quarantine') == quarantine:
            quarantined.append(key)
            continue
        if (active and active['source_sha256'] != entry.get('source_sha256')):
            deferred.append({'key': key, 'reason': 'Reader/source generation conflict; existing text protected'})
            continue
        has_effective = active and active.get('components', {}).get('text') == 'ready'
        if not entry.get('text_layer') and not has_effective:
            deferred.append({'key': key, 'reason': 'complete verified text companion not yet published by Reader'})
            continue
        if old and old['source_sha256'] == entry.get('source_sha256'):
            retained = old['retained_reader_resources']
            if ((has_effective and active['resource'] in retained)
                    or (not has_effective and entry.get('text_layer') in retained)):
                continue
        jobs.append({**identity, 'book_id': book, 'fingerprint': fingerprint, 'quarantine': quarantine})
    jobs.sort(key=lambda job: (job['book_id'] in exports,
                              job['entry'].get('page_count', 1 << 63), job['key']))
    return jobs, deferred, quarantined


def export_spec(store, job, number):
    entry, key = job['entry'], job['key']
    repo, path = key.split('\0')
    if entry.get('repo') != repo or entry.get('path') != path:
        raise ValueError('OCR registry original source-key mismatch')
    spec = {'origin_repo': repo, 'origin_rel_path': path, 'origin_revision': entry.get('source_revision'),
            'origin_sha256': entry.get('source_sha256'), 'source_sha256': entry.get('source_sha256'),
            'page_count': entry.get('page_count'), 'doc_number': number, 'text_manifest': entry.get('text_layer')}
    active = job.get('active')
    if active and active.get('components', {}).get('text') == 'ready':
        ref = active['resource']
        manifest = reading.v3.validate_reading_manifest(reading.v3.decode(publisher.verified_remote(store, ref)))
        if (manifest['source_key'] != key or manifest['source_sha256'] != spec['source_sha256']
                or manifest['page_count'] != spec['page_count']
                or manifest['generation'] != active['reading_generation']):
            raise ValueError('effective Reader/OCR identity mismatch')
        spec.update(reading_manifest=ref, text_manifest=manifest.get('text_layer'))
    if entry.get('source_kind', 'upstream') != 'upstream':
        derivation = job.get('derivation')
        bucket = entry.get('reader_assets_bucket')
        relative = entry.get('reader_assets_path')
        if derivation is None and isinstance(relative, str):
            match = re.fullmatch(r'documents/pdf/[a-z0-9_-]+/([0-9a-f]{64})/document\.pdf', relative)
        else:
            match = None
        if match:
            # Static PDF/Office/PostScript conversions keep their provenance in
            # a document-owned manifest next to the PDF instead of the derived
            # source index. The directory name is the original source digest.
            if bucket not in {shared.PDF_PAGES_BUCKET, shared.READER_ASSETS_BUCKET}:
                raise ValueError('unsupported derived provenance bucket')
            manifest_path = relative.rsplit('/', 1)[0] + '/document-manifest.json'
            value = json.loads(store.read_bytes(bucket, manifest_path))
            if (value.get('kind') != 'static-pdf-document-stream' or value.get('version') != 1
                    or value.get('path') != 'document.pdf'
                    or value.get('source_sha256') != match.group(1)
                    or value.get('sha256') != spec['source_sha256']
                    or value.get('bytes') != entry.get('source_bytes')):
                raise ValueError('derived OCR lacks matching original provenance')
            derivation = {'bucket': bucket, 'manifest_path': manifest_path,
                          'source_sha256': value['source_sha256'],
                          'derived_sha256': value['sha256'],
                          'derived_bytes': value['bytes']}
        if (not derivation or derivation.get('derived_sha256') != spec['source_sha256']
                or derivation.get('derived_bytes') != entry.get('source_bytes')):
            raise ValueError('derived OCR lacks matching original provenance')
        if derivation.get('repo') is not None or derivation.get('source_path') is not None:
            # The derived-source index carries a full original identity; require
            # it to match. A document-owned manifest has no repo/path fields and
            # binds the conversion through the source digest instead.
            if (derivation.get('repo') != repo or derivation.get('source_path') != path
                    or derivation.get('source_revision') != spec['origin_revision']):
                raise ValueError('derived OCR lacks matching original provenance')
        bucket, relative = derivation.get('bucket'), contract.safe_path(derivation.get('manifest_path'))
        if bucket not in {shared.PDF_PAGES_BUCKET, shared.READER_ASSETS_BUCKET}:
            raise ValueError('unsupported derived provenance resource')
        if not (relative.startswith(('derived/', 'objects/'))
                or re.fullmatch(r'documents/pdf/[a-z0-9_-]+/[0-9a-f]{64}/document-manifest\.json', relative)):
            raise ValueError('unsupported derived provenance resource')
        raw = store.read_bytes(bucket, relative)
        spec.update(origin_sha256=derivation.get('source_sha256'), derivation_manifest={
            'bucket': bucket, 'path': relative, 'sha256': contract.digest(raw), 'bytes': len(raw), 'role': 'provenance'})
    exporter.source_identity(spec)
    reading.v3.resource(spec['text_manifest'])
    return spec


def save_state(store, state):
    publisher.assert_writer()
    publisher.put_checked(store, STATE, contract.canonical(state))


def reconcile(store, root, consumer_root, approved, *, limit=8, apply=False, plan_only=False,
              retry_failures=False, activate=False, source=None, report=None, rebase=False):
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError('OCR batch must select 1..32 complete books, never page prefixes')
    approved = configuration(approved)
    if apply:
        publisher.assert_writer()
    root.mkdir(parents=True, exist_ok=True)
    pointer, refs, exports = current_exports(store, root)
    if pointer and pointer['base_metadata_sha256'] != approved['base_metadata_sha256']:
        if not rebase:
            raise ValueError('OCR automation base differs from the approved consumer')
        report = report if report is not None else {}
        report.update(applied=apply, plan_only=plan_only, model_calls=0, deleted_objects=0,
                      prior_documents=len(exports), rebase=True,
                      base_metadata_sha256=approved['base_metadata_sha256'],
                      source_scope='rebase-live-exports')
        if plan_only:
            report['selected'] = []
            return report
        release = builder.build_release(root, list(refs.values()), consumer_root,
                                        approved['builder_sha256'], approved['base_metadata_sha256'])
        (root / 'release-receipt.json').write_bytes(contract.canonical(release))
        if activate:
            publisher.activate_space(release['path'], release['sha256'],
                                     approved['base_metadata_sha256'], approved['builder_sha256'])
        report['publication'] = publisher.publish(
            store, root, release['path'], release['sha256'],
            approved['base_metadata_sha256'], pointer['generation'], apply=apply)
        report.update(generation=report['publication']['generation'],
                      release=report['publication']['release'], documents=len(exports))
        if activate and apply:
            report.update(publisher.activate_space(
                release['path'], release['sha256'], approved['base_metadata_sha256'],
                approved['builder_sha256'], apply=True))
        return report
    registry = json.loads(store.read_bytes(shared.READER_ASSETS_BUCKET, REGISTRY))
    derived = json.loads(store.read_bytes(shared.PDF_PAGES_BUCKET, DERIVED))
    _reader_pointer, catalog = reading.current(store)
    state, _state_raw = publisher.optional_json(store, STATE)
    state = state or {'version': 1, 'kind': 'ccrd-ocr-automation', 'failures': {}, 'resources': []}
    if state.get('kind') != 'ccrd-ocr-automation' or not isinstance(state.get('failures'), dict):
        raise ValueError('invalid durable OCR quarantine')
    state = copy.deepcopy(state)
    jobs, deferred, quarantined = discover(registry, derived, catalog, exports, state, retry_failures=retry_failures)
    if source:
        if not isinstance(source, dict) or set(source) != {'repo', 'path'}:
            raise ValueError('exact automatic pilot source requires repo and path')
        key = source['repo'] + '\0' + source['path']
        if key not in registry['files']:
            raise ValueError('exact automatic pilot source is not in the OCR registry')
        jobs = [job for job in jobs if job['key'] == key]
        if not jobs:
            raise ValueError('exact automatic pilot source is unchanged, deferred or quarantined')
    report = report if report is not None else {}
    report.update(applied=apply, plan_only=plan_only, model_calls=0, deleted_objects=0,
                  prior_documents=len(exports), eligible=len(jobs), batch_limit=limit,
                  deferred=deferred, quarantined=quarantined, processed=[], failures=[])
    report['source_scope'] = source or 'all-ready-complete-text'
    if pointer:
        report.update(generation=pointer['generation'], release=pointer['release'])
    if plan_only:
        report['selected'] = [job['key'] for job in jobs[:limit]]
        return report
    registry_ids, _ = publisher.optional_json(store, publisher.IDS)
    ids = (registry_ids or {'ids': {}})['ids']
    for book, value in exports.items():
        if ids.get(book) != {'doc_id': value['doc_id'], 'readable_rel_path': value['readable_rel_path']}:
            raise ValueError('live OCR export differs from the stable ID registry')
    next_id = max([0, *[int(item['doc_id'].split(':')[1]) for item in ids.values()]]) + 1
    changed = 0
    for job in jobs:
        if changed == limit:
            break
        old = exports.get(job['book_id'])
        number = int(ids[job['book_id']]['doc_id'].split(':')[1]) if job['book_id'] in ids else next_id
        try:
            spec = export_spec(store, job, number)
            result = exporter.build_export(spec, lambda ref: store.read_bytes(ref['bucket'], ref['path']), allow_unreviewed=True)
            value = result['manifest']
            if old and value['generation'] == old['generation']:
                continue
            require_retained(store, value)
            if old:
                publisher.verify_text_upgrade(store, old, value)
            else:
                next_id = max(next_id, number + 1)
            if any(item['readable_rel_path'] == value['readable_rel_path'] and book != job['book_id']
                   for book, item in exports.items()):
                raise ValueError('OCR readable path collision; neither book may be overwritten')
            for path, raw in result['files'].items():
                store_local(root, publisher.resource(path, raw), raw)
            raw = result['files'][result['manifest_path']]
            ref = publisher.resource(result['manifest_path'], raw, 'provenance')
            contract.export_record(root, ref)
            refs[job['book_id']], exports[job['book_id']] = ref, value
            state['failures'].pop(job['key'], None)
            report['processed'].append({'key': job['key'], 'doc_id': value['doc_id'],
                'page_count': value['page_count'], 'quality': value['quality'], 'accepted_pages': value['accepted_pages'],
                'text': value['text'], 'export': ref, 'updated': old is not None})
            changed += 1
        except Exception as error:
            # Quarantine only this exact input identity. Keep the old complete
            # release active; the next automatic run can advance other books.
            permanent = isinstance(error, (exporter.PermanentTextRejection, StaleRetainedProvenance))
            failure = {'key': job['key'], 'fingerprint': job['fingerprint'],
                       'quarantine': job['quarantine'], 'error_type': type(error).__name__}
            if permanent:
                # The source's own text is unusable (binary controls, no usable
                # characters). Record it as a terminal disposition instead of
                # failing every later batch for the same unchanged bytes.
                failure['rejected'] = str(error)[:300]
            report['failures'].append(failure)
            state['failures'][job['key']] = failure
            if apply:
                save_state(store, state)
            if apply and permanent:
                continue
            raise
    report['documents'] = len(exports)
    if not changed:
        report['unchanged'] = True
        if activate and pointer:
            report.update(publisher.activate_space(pointer['release']['path'], pointer['release']['sha256'],
                approved['base_metadata_sha256'], approved['builder_sha256'], apply=apply))
        return report
    release = builder.build_release(root, list(refs.values()), consumer_root,
                                    approved['builder_sha256'], approved['base_metadata_sha256'])
    (root / 'release-receipt.json').write_bytes(contract.canonical(release))
    if activate:
        publisher.activate_space(release['path'], release['sha256'], approved['base_metadata_sha256'], approved['builder_sha256'])
    report['publication'] = publisher.publish(store, root, release['path'], release['sha256'],
        approved['base_metadata_sha256'], pointer['generation'] if pointer else None, apply=apply)
    report.update(generation=report['publication']['generation'], release=report['publication']['release'])
    if apply and state != (json.loads(_state_raw) if _state_raw else {'version': 1, 'kind': 'ccrd-ocr-automation', 'failures': {}, 'resources': []}):
        save_state(store, state)
    if activate:
        report.update(publisher.activate_space(release['path'], release['sha256'], approved['base_metadata_sha256'],
                                               approved['builder_sha256'], apply=apply))
    return report


def prepare_consumer(approved, root):
    from huggingface_hub import hf_hub_download
    import os
    for name in ('build_fulltext_db.py', 'data/search_data.json.gz', 'ocr_release.py'):
        source = Path(hf_hub_download('vomebook/Search', name, repo_type='space',
            revision=approved['consumer_revision'], token=os.environ.get('HF_TOKEN')))
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    if (contract.digest((root / 'build_fulltext_db.py').read_bytes()) != approved['builder_sha256']
            or contract.digest((root / 'data/search_data.json.gz').read_bytes()) != approved['base_metadata_sha256']
            or (root / 'ocr_release.py').read_bytes() != Path(contract.__file__).read_bytes()):
        raise ValueError('pinned consumer differs from approved builder/base/contract')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('ccrd-ocr-consumer.json'))
    parser.add_argument('--root', type=Path, default=Path('output/ccrd-ocr-auto/candidate'))
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--retry-failures', action='store_true')
    parser.add_argument('--rebase', action='store_true',
                        help='Rebuild a full OCR release when only the base metadata changed')
    parser.add_argument('--source-json', default='{}', help='Optional exact repo/path for a bounded automatic pilot')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--activate-space', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.plan_only and args.apply:
        raise ValueError('plan-only cannot apply')
    approved = configuration(json.loads(args.config.read_text()))
    consumer = args.root.parent / 'consumer'
    report = {}
    try:
        if not args.plan_only:
            prepare_consumer(approved, consumer)
        reconcile(publisher.HubBucketStore(), args.root, consumer, approved, limit=args.limit,
                  plan_only=args.plan_only, retry_failures=args.retry_failures, apply=args.apply,
                  activate=args.activate_space, source=json.loads(args.source_json), report=report,
                  rebase=args.rebase)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(contract.canonical(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
