"""Exact first-cohort same-source EPUB publication, only under the central writer fence."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic
from uuid import uuid4

import httpx
from huggingface_hub import HfApi

from scripts import publish_reader_v3 as v3, reader_image_history_publish as legacy
from scripts import reader_ebook_rebuild as rebuild, reader_ebook_rebuild_archive as archive
from scripts import reader_ebook_rebuild_preflight as preflight, reader_ebook_rebuild_review as review
from scripts import reader_ebook_recovery_inputs as recovery_inputs, reader_ebook_rebuild_recover as recover
from scripts import reader_web_managed_pages as pages, reader_epub_shadow as source_io
from scripts.audit_epub_links import compare_bundle_source
from scripts.reader_assets import source_url
from scripts.reader_ebook_rebuild_transport import GuardedTransport
from scripts.reader_image_history_parent import CatalogParent
from scripts.reader_image_history_publish_s3 import LegacyRoots
from scripts.reader_image_history_store import S3ClaimStore
from scripts.reader_page_cas import ConditionalPageStore
from scripts.reader_stream_checkpoint_cas import ConditionalCheckpointStore
from scripts.reader_web_exact_apply import guard_window

B = v3.ASSETS
REVIEW_PREFIX = 'reader-index/gc/ebook-exact-reviews/v1/'


def sha_pin(value):
    return isinstance(value, str) and rebuild.backlog.SHA.fullmatch(value)


def prepare(options, directory, *, hub, s3, client, archive_bytes, authority, verify_source):
    """Recreate the exact read-only proposal at the current qualified parents."""
    bundles = archive.unpack(archive_bytes, options['archive_sha256'], directory)
    anchors = {}
    pointer, _ = v3.current(hub)
    review.require(pointer and pointer['generation'] == options['reader_generation'], 'Reader parent changed before EPUB preparation')
    plan = verify_source(options)
    proof = authority(pointer)
    review.require(proof['search_generation'] == plan['search_generation'], 'Pages/Search identity differs')
    sources = {row['key']: row for row in plan['rows'] if row['key'] in bundles}
    review.require(set(sources) == set(bundles), 'candidate EPUB sources missing from exact plan')
    text_proofs = {}
    with TemporaryDirectory(dir=directory) as temp:
        for n, (key, bundle) in enumerate(sorted(bundles.items())):
            row = sources[key]
            source = Path(temp)/f'{n}.epub'
            source_io.download(source_url(row['repo'], row['revision'], row['path']), source, None)
            with source.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            review.require(source.stat().st_size == row['bytes'] and digest == row['source_sha256'],
                           'exact EPUB original differs from pinned identity')
            source_io.validate_archive(source)
            text = compare_bundle_source(source, bundle)
            review.require(not text['differences'] and text['source_chars'] > 0, 'candidate EPUB differs from original visible text')
            text_proofs[key] = text
    controls = ConditionalPageStore(hub, s3=s3)
    cps = ConditionalCheckpointStore(s3)
    legacy_roots = LegacyRoots(client)
    as_of = options.get('timestamp') or datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    operation = options.get('operation') or uuid4().hex

    def catalog_input(out):
        observed = legacy_roots.observe(legacy.CATALOG)
        catalog = CatalogParent(client, digest=observed.sha256, size=observed.size, etag=observed.etag)
        catalog._download(legacy.CATALOG, out, condition=observed.etag)
        anchors.update(catalog=catalog)
        return observed.sha256, observed.size

    data = {}
    result = preflight.preflight(hub, plan, bundles, operation=operation, timestamp=as_of,
                                  open_catalog=catalog_input, capture=data)
    index = cps.read(B, review.INDEX)
    sidecar, sidecar_etag = controls.read_for_update(B, legacy.SIDECAR)
    parent_raw, pointer_etag = controls.read_for_update(B, v3.POINTER)
    review.require(index.raw == data['index'] and sidecar == data['sidecar']
                   and json.loads(parent_raw) == pointer and hub.read_bytes(B,v3.POINTER) == parent_raw,
                   'exact EPUB preparation parents disagree')
    review.require(authority(pointer, expected=proof) == proof, 'Pages authority moved during EPUB preparation')
    sealed = {**result, 'archive_sha256': options['archive_sha256'], 'source_revision': options['source_revision'],
              'pages_authority': proof, 'original_text': text_proofs, 'pointer_parent_sha256': review.sha(parent_raw),
              'parent_etags': {'catalog': anchors['catalog'].etag, 'canonical': index.etag,
                               'sidecar': sidecar_etag, 'pointer': pointer_etag}}
    data.update(parent_raw=parent_raw, parent_catalog=anchors['catalog'], bundles=bundles)
    return sealed, data


def release_guard(hub, parent, target, proof, *, fence=guard_window, now=monotonic):
    """Check the independent writer fence at transitions and during bounded staging batches.

    The operator's continuous isolation and the central workflow locks are still
    required. Reuse a verified fence only for immutable members under protected
    roots, never for a journal, mutable root, or potentially unprotected write.
    """
    last_member_check = None
    member_writes = 0

    def guard(phase):
        nonlocal last_member_check, member_writes
        member = phase == 'stage:member-put'
        tick = now()
        if member and last_member_check is not None and 0 <= tick - last_member_check < 20 and member_writes < 16:
            member_writes += 1
            return True
        last_member_check, member_writes = None, 0
        if fence(hub, True) is not True:
            return False
        current, _ = v3.current(hub)
        if current != parent:
            return current == target and (phase in {'recovery:inspect', 'stage:observe-begin', 'stage:observe-readback'}
                                          or phase.startswith('v3:'))
        if phase in {'archive', 'stage:begin', 'canonical:begin', 'legacy:begin', 'v3:begin', 'v3:write-or-readback'}:
            if pages.current(parent, expected=proof) != proof:
                return False
        if member:
            last_member_check, member_writes = now(), 1
        return True

    return guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resource-json', required=True)
    parser.add_argument('--output', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--recover', choices=('inspect','canonical','legacy-start','legacy-resume','v3','pages'))
    args = parser.parse_args()
    opts = json.loads(args.resource_json)
    fields = {'archive_sha256','reader_generation','source_revision','search_generation'}
    if args.apply or args.recover:
        fields |= {'review_sha256','external_writers_stopped'}
    review.require(isinstance(opts, dict) and set(opts) == fields and
                  all(sha_pin(opts[k]) for k in fields - {'source_revision','external_writers_stopped'})
                  and isinstance(opts['source_revision'], str) and rebuild.backlog.REV.fullmatch(opts['source_revision'])
                  and (not (args.apply or args.recover) or opts['external_writers_stopped'] is True),
                  'exact EPUB release scope, hashes and writer confirmation required')
    review.require(os.getenv('GITHUB_REPOSITORY') == 'anftm/pipeline' and
                   os.getenv('GITHUB_EVENT_NAME') == 'workflow_dispatch' and os.getenv('GITHUB_RUN_ATTEMPT') == '1'
                   and os.getenv('GITHUB_REF') == 'refs/heads/main', 'exact hosted EPUB publication required')
    hub = v3.CentralHubStore()
    s3 = ConditionalCheckpointStore().store
    client = s3._client('vomebook')
    claims = S3ClaimStore(client)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('output must be a new path')
    if args.apply or args.recover:
        approved, _ = claims._get(REVIEW_PREFIX+opts['review_sha256']+'.json', maximum=8*1024**2)
        review.require(approved and review.sha(approved) == opts['review_sha256'], 'exact EPUB release review is not staged')
        expected = json.loads(approved)
        review.require(expected.get('archive_sha256') == opts['archive_sha256'] and
                       expected.get('reader_generation') == opts['reader_generation'] and
                       expected.get('source_revision') == opts['source_revision'] and
                       expected['pages_authority']['search_generation'] == opts['search_generation'],
                       'review scope differs from dispatch inputs')
        opts.update(operation=expected['operation'], timestamp=expected['timestamp'])
        guard_window(hub, True)
    else:
        expected = None
    if args.recover:
        parents = recovery_inputs.load(claims, hub, expected, opts['review_sha256'])
        proof = expected['pages_authority']
        guard = release_guard(hub, json.loads(parents['pointer']), expected['target_pointer'], proof)
        if args.recover not in {'inspect','pages'}:
            fresh_search = rebuild.backlog.capture_search(hub)
            review.require(json.loads(fresh_search[1])['generation'] == opts['search_generation']
                           and fresh_search[3].get(rebuild.REPO) == opts['source_revision']
                           and HfApi().repo_info(rebuild.REPO, repo_type='dataset', timeout=30).sha == opts['source_revision'],
                           'Search or dataset pin changed before recovery')
        answer = recover.run(GuardedTransport(s3, hub, guard=guard), expected, opts['review_sha256'], parents, args.recover)
        with args.output.open('xb') as out:
            out.write(v3.encode(answer))
        return
    raw, _ = claims._get(archive.PREFIX+opts['archive_sha256']+'.zip', maximum=archive.MAX_ZIP)
    review.require(raw is not None, 'exact EPUB candidate archive is not staged')

    def verify_source(inputs):
        with httpx.Client(timeout=60, follow_redirects=True) as http:
            return rebuild.capture(generation=inputs['search_generation'], revision=inputs['source_revision'],
                                   reader_generation=inputs['reader_generation'], store=hub,
                                   api=HfApi(), client=http)

    with TemporaryDirectory(prefix='reader-ebook-release-') as tmp:
        sealed, data = prepare(opts, Path(tmp), hub=hub, s3=s3, client=client, archive_bytes=raw,
                               authority=pages.current, verify_source=verify_source)
        if not args.apply:
            answer = sealed
        else:
            review.require(v3.encode(sealed) == v3.encode(expected), 'exact EPUB live review differs from approved bytes')
            proof = sealed['pages_authority']
            guard = release_guard(hub, data['pointer'], sealed['target_pointer'], proof)
            transport = GuardedTransport(s3, hub, guard=guard)
            path = data['parent_catalog']
            catalog_parent = legacy.Version(path.digest, path.size, path.etag)
            with TemporaryFile() as catalog:
                path.stage(catalog, guard=lambda: guard('archive'))
                # Reopen recovery inputs from their durable location before the first permit.
                path.load(catalog)
                recovery_inputs.retain(claims, hub, sealed, opts['review_sha256'], index=data['index'],
                                       sidecar=data['sidecar'], pointer=data['parent_raw'], guard=guard)
                staged = transport.stage(review=sealed, review_sha256=opts['review_sha256'],
                                         bundles=data['bundles'], after_sidecar=data['target_sidecar'])
                index = transport.canonical(review=sealed, review_sha256=opts['review_sha256'], staged=staged,
                                            parent_raw=data['index'], parent_etag=sealed['parent_etags']['canonical'])
                result = transport.legacy(review=sealed, parent_catalog=catalog, parent=catalog_parent,
                                          before_sidecar=data['sidecar'], after_sidecar=data['target_sidecar'],
                                          sidecar_etag=sealed['parent_etags']['sidecar'], parent_raw=data['parent_raw'])
            result = transport.promote(review=sealed, parent_raw=data['parent_raw'], legacy_result=result)
            answer = {**result, 'applied': True, 'canonical': index, 'changed_rows': len(sealed['changes']),
                      'pages_pending': True, 'changes': sealed['changes'],
                      'pages_pins': {'code_sha': proof['code_sha'], 'search_generation': proof['search_generation']}}
    with args.output.open('xb') as out:
        out.write(v3.encode(answer))


if __name__ == '__main__':
    main()
