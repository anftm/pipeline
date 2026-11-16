"""Release eight superseded PDF processing protections; never delete or resume.

The original nine-root retirement and exact replacement proofs are pinned to the
reviewed 43803742299 operation. Recovery is read-only inspection of the immutable
intent; a partial attempt must not be rerun. Maintenance restores producers even
on failure because this operation neither deletes bytes nor changes publication.
"""
import argparse
import copy
from functools import partial
import json
import os
from pathlib import Path
import re

import httpx

from . import publish_reader_v3 as publication, reader_lifecycle as lifecycle
from . import reader_gc_graph as graph, reader_gc_maintenance as maintenance
from . import reader_gc_chm_window, reader_obsolete_pdf_apply as operation
from .reader_route_control import prepare_base
from .reader_gc_collect import read_catalog_stream
from .reader_gc_stream import catalog_json
from .reader_bucket_store import HubBucketStore
from .reader_gc_store import CollectionStore
from .reader_gc_fence import GitHubProducerFence
from .reader_page_history_store import ConditionalPageHistoryStore

p = operation.preparation
require, proof = p.require, p.proof
ASSETS, BUCKET = operation.ASSETS, operation.BUCKET
INTENTS = 'reader-index/gc/obsolete-pdf-processing-handoff/'
KIND = 'reader-obsolete-pdf-processing-handoff-intent'
REFERENCE_INTENT = {'bucket': ASSETS,
    'path': operation.INTENTS + 'f460f006aa432529b2f5c11d5208c7162b4197e20432d9670cc410c6dafe3954.json',
    'bytes': 905461, 'sha256': 'f460f006aa432529b2f5c11d5208c7162b4197e20432d9670cc410c6dafe3954'}
REPLACEMENTS_SHA256 = 'e2c53a3513e2a7ddaf072898f6c338bf8b192cde69adf5210a926318efffbc06'
# Observed final30-processing-root-pins.json: exact path, resource, and canonical
# document hash (publication.encode, not a hash of the inventory's whitespace).
APPROVED = {
    'reader-index/processing/ccrd-body-rebuild-d0d56b80639bf808-20261025.json': (
        'derived/migration-20261007/6f9f9f251a929f25e1b5be3beb32ec60',
        '5ad0791dd2462717a85568a736732b47bbf20bb916195f2cd7bef53f226ecd6f'),
    'reader-index/processing/pdf-derived-136bb44fec8b51518b3cf73f01832dccbf6d4082ffe53a8459ab4217f77367e6.json': (
        'objects/18/18eb6ed202847a0d3c9546d67f1d111c5bc8cf1ac07dbad8bc82deb2d25e43e6/1062ec87146af458',
        'b7a3d74a775ccda1ce3b16b662ec25a742b66424bf2edb402c26e004e6946c2a'),
    'reader-index/processing/pdf-derived-1ab215e08124855867cfae86689f4d80b12d739aa3893e84cb507b55c2995286.json': (
        'objects/e6/e6de122aab314b066bb94184f9b9c037252689fd812e4b5e5d0c09f2f8ba5b42/0720c3567509ab3b',
        'd3cd912b5f26d0744c41067f9265175b8b802618a57a3aa8b229fc10177a8760'),
    'reader-index/processing/pdf-derived-4028e9a92fd057d79dcf06f0333af2f9adf65a051f78efd7876c6b43c5106cc2.json': (
        'objects/03/033ccf2294bb1b5a78d19e2315e6f523e732520055215fd7f0102cf4754cdbcb/284e0bbb3f157bf8',
        'd6582b9ef11272f1c8ebc697ebfd6a50a3d0a3ea09f9626295feb170fb2e095d'),
    'reader-index/processing/pdf-derived-63347f5801044fb2f2c6e4dddf119aa73844c4798c9e927b0733dd75a0195215.json': (
        'objects/98/989eb1c6b6a18c60c9e0b71777cf1ba4676aadff197d5846faa3e2709dd3a44a/7102c6ae9dc7bcd6',
        '8adea2ac9bb536ba6128c02a16235a37b83fb00e38922c5d5166384b468c6fd2'),
    'reader-index/processing/pdf-derived-7af90339d5c8e308c750041c8248849cce6c8762259d5a9ca7164e47e674839c.json': (
        'objects/ab/abfad6543a1567cba5300769e9ffb722ea70c6bf52344413cb73ec6d8e795e6b/47492fa600512e8b',
        '0711d484d0464be13476929788915e2c1f0bf28b6c9dc3e92643820bdf016e7f'),
    'reader-index/processing/pdf-derived-c5a41fb330b75e432a8dcbbc6129d7ee6235090637cbfe6b4b003212c1577f8d.json': (
        'objects/61/61683df191668b0fc1a7b0f1772d9f77af05922157ae7902ee630ff5a2c05d32/30ac520e0fd8e2b9',
        '9b6de664663e1390f28f45c0b1fcf807ffb4a2235a397143b43d0d7b1b6e0db2'),
    'reader-index/processing/pdf-derived-e77178ac6e5675ad365e7df9158272183ca888622c4eb067c088c807f13bed95.json': (
        'objects/ce/cea8b04b5a2ee0e31303fa1fc70024a79317ec7d67ad4c51a56aac81c8cd8428/e50ead8ce0974043',
        '66f6b2e75774810a1dde49e2f1697d44ff514f871c490f6b3f149d2ed2bb2e67'),
}


def validate_config(config):
    require(set(config) == {'processing_records', 'reference_intent', 'replacement_proofs'},
            'exact handoff config fields required')
    require(config['reference_intent'] == REFERENCE_INTENT, 'unapproved reference intent')
    require(proof(publication.encode(config['replacement_proofs']))['sha256'] == REPLACEMENTS_SHA256,
            'replacement proofs differ from exact reviewed plan')
    rows = config['processing_records']
    require(len(rows) == 8 and {r['path'] for r in rows} == set(APPROVED),
            'expected all eight approved processing paths')
    for row in rows:
        root, digest = APPROVED[row['path']]
        document = row['document']
        resources = [{'bucket': BUCKET, 'root': root}]
        require(document.get('resources') == resources and row.get('matching_resources') == resources,
                'processing resource mapping differs')
        require(proof(publication.encode(document))['sha256'] == digest,
                'processing record differs from approved observation')


def reference_after(store, config):
    inspection = operation.inspect(store, config['reference_intent'])
    require(len(inspection['roots']) == 9 and all(r['outcome'] == 'after' for r in inspection['roots']),
            'original nine reference roots are not all after')
    raw = store.read_bytes(ASSETS, config['reference_intent']['path'])
    require(proof(raw) == {k: config['reference_intent'][k] for k in ('bytes', 'sha256')},
            'reference intent changed')
    return json.loads(raw)


def verify_replacements(store, config, intent, metadata, projection):
    """Bind all 30 targets to eight source pairs and their exact live replacements."""
    mutations = {(m['bucket'], m['path']): m for m in
                 (operation._unpacked(row) for row in intent['mutations'])}
    roots = {(r['bucket'], r['path']): r for r in intent['root_proofs']}
    targets = intent['target_delete_paths']
    require(len(targets) == len(set(targets)) == 30, 'expected thirty obsolete targets')
    target_roots = {path.rsplit('/', 1)[0] for path in targets}
    require(len(target_roots) == 15 and set(targets) == {
        root + '/' + name for root in target_roots for name in ('document.pdf', 'derived-manifest.json')},
        'obsolete targets are not exact PDF/manifest pairs')

    def original_manifest(path):
        raw = store.read_bytes(BUCKET, path)
        expected = roots[BUCKET, path]
        require(proof(raw) == {k: expected[k] for k in ('bytes', 'sha256')},
                'original source manifest changed')
        return json.loads(raw)

    legacy = [original_manifest(root + '/derived-manifest.json') for root in sorted(target_roots)
              if root.startswith('derived/')]
    require(len(legacy) == 8 and len({m['key'] for m in legacy}) == 8, 'expected eight legacy sources')
    by_key = {r['key']: r for r in config['replacement_proofs']}
    require(len(by_key) == 8 and set(by_key) == {m['key'] for m in legacy}, 'replacement source keys differ')
    resource_roots, covered = set(), set()
    for old in legacy:
        p.audit.validate_entry(old)
        replacement = by_key[old['key']]
        pdf, manifest = replacement['pdf'], replacement['manifest']
        path = pdf['path']
        require(pdf['bucket'] == BUCKET and path not in targets
                and manifest['path'] == path.rsplit('/', 1)[0] + '/derived-manifest.json',
                'replacement object identity differs')
        match = p.audit.NEW.fullmatch(path)
        require(match and match[1] == match[2][:2] and match[2] == old['source_sha256'],
                'replacement source hash differs')
        raw = store.read_bytes(BUCKET, manifest['path'])
        mutation = mutations.get((BUCKET, manifest['path']))
        proof_raw = mutation['before'] if mutation else raw
        require(proof(proof_raw) == {k: manifest[k] for k in ('bytes', 'sha256')}
                and (mutation is None or raw == mutation['after']), 'replacement manifest changed')
        current = json.loads(raw)
        require(all(current.get(k) == old[k] for k in p.SOURCE)
                and current.get('new_path') == path and current.get('manifest_path') == manifest['path']
                and current.get('derived_bytes') == pdf['bytes']
                and current.get('derived_sha256') == pdf['sha256'], 'replacement source identity differs')
        require(metadata(BUCKET, path) == pdf['metadata'], 'active PDF metadata changed')
        require(proof(store.read_bytes(BUCKET, path)) == {k: pdf[k] for k in ('bytes', 'sha256')},
                'active PDF content changed')
        require(projection['f'].get(old['key']) == {'s': 2, 'm': 'p', 'p': path, 'b': BUCKET},
                'replacement is not the current pinned route')
        old_root = old['new_path'].rsplit('/', 1)[0]
        covered.add(old_root)
        if old['source_extension'] == 'caj':
            require(mutation is None and p.audit.object_manifest_matches(old, path, current),
                    'CAJ replacement differs from exact copy')
            resource_roots.add(old_root)
        else:
            require(mutation is not None and old['profile'] == 'gbk-font-repair-v1'
                    and current['profile'] == current['generation'] == 'gbk-font-repair-v2',
                    'PDF replacement is not the verified v2 repair')
            copies = [root for root in target_roots if root.startswith('objects/')
                      and root.split('/')[2] == old['source_sha256']]
            require(len(copies) == 1, 'obsolete copy source mapping differs')
            copy_root = copies[0]
            require(p.audit.object_manifest_matches(old, copy_root + '/document.pdf',
                    original_manifest(copy_root + '/derived-manifest.json')), 'obsolete copy source differs')
            covered.add(copy_root)
            resource_roots.add(copy_root)
    require(covered == target_roots and resource_roots == {v[0] for v in APPROVED.values()},
            'eight processing resources do not map exactly to thirty obsolete targets')


def observe(store, client, config, metadata):
    intent = reference_after(store, config)
    pointer, catalog = publication.current(store)
    require(pointer and catalog.get('base_sidecar'), 'current pinned Reader generation required')
    pinned = publication.pinned_base(store, catalog, None)
    canonical_raw = store.read_bytes(ASSETS, p.audit.SIDECAR)
    canonical = graph.decode(canonical_raw)
    prepared_base, _ = prepare_base(catalog, canonical_raw)
    require(graph.decode(prepared_base) == pinned, 'canonical sidecar differs from full pinned base')
    projection = publication.project_sidecar(pinned, catalog)
    observations = {site: publication.observe_consumer(store, site, client) for site in ('hf', 'pages')}
    for site, receipt in observations.items():
        require(receipt.get('surface') == site and receipt.get('active') is True
                and receipt.get('projection_verified') is True and receipt.get('checked_url')
                and receipt.get('checked_at') and receipt.get('generation') == pointer['generation']
                and receipt.get('catalog_sha256') == pointer['catalog']['sha256']
                and receipt.get('files') == len(catalog['files']), 'unverified current consumer: ' + site)
    reference = graph.ReferenceGraph(store)
    # Graph traversal deduplicates across live entries; ownership validation
    # needs the current entry's exact references, including shared resources.
    base, base_digest = read_catalog_stream(store, partial(catalog_json, deduplicate_references=False))
    require(base and base.get('version') == 1 and base.get('kind') == 'reader-catalog',
            'invalid current legacy catalog')
    reference.merge_catalog(base)
    generation = base.get('current', {}).get('generation')
    entry = base.get('generations', {}).get(generation)
    require(isinstance(entry, dict) and entry.get('generation') == generation
            and entry.get('status') == 'current' and reference.handoff_resolved(generation)
            and entry.get('sidecar_sha256') == proof(publication.encode(canonical))['sha256']
            and 'redirect_sidecar_pending' not in base, 'current base generation is not resolved and bound')
    refs = entry.get('references')
    require(isinstance(refs, list) and all(isinstance(ref, dict)
            and isinstance(ref.get('bucket'), str) and isinstance(ref.get('path'), str) for ref in refs),
            'current legacy references are invalid')
    require({(ref['bucket'], ref['path']) for ref in lifecycle.sidecar_references(canonical)}
            <= {(ref['bucket'], ref['path']) for ref in refs},
            'current legacy references do not cover canonical sidecar')
    verify_replacements(store, config, intent, metadata, projection)
    require(publication.current(store)[0] == pointer, 'Reader pointer changed')
    return {'generation': generation, 'pointer': pointer, 'observations': observations,
            'base_catalog_sha256': base_digest,
            'projection_identity': proof(publication.encode(projection)),
            'reference_intent': config['reference_intent'], 'replacement_proofs': config['replacement_proofs']}


def prepare(store, config, evidence, now=None):
    mutations = []
    for row in sorted(config['processing_records'], key=lambda r: r['path']):
        raw = store.read_bytes(ASSETS, row['path'])
        record = json.loads(raw)
        require(record == row['document'], 'processing record changed: ' + row['path'])
        released = lifecycle.release_processing(record, evidence['generation'], now)
        # Graph explicitly ignores observations, even if generation resolution
        # later fails. Never claim the obsolete resources were newly published.
        released['observations'] = {'obsolete_pdf_handoff': {
            'kind': 'superseded-verified-replacement', 'reference_intent': evidence['reference_intent'],
            'replacement_proofs_sha256': REPLACEMENTS_SHA256,
            'replacement_generation': evidence['pointer']['generation'],
            'base_catalog_sha256': evidence['base_catalog_sha256']}}
        after = publication.encode(released)
        mutations.append({'bucket': ASSETS, 'path': row['path'], 'before': raw, 'after': after,
                          'before_identity': proof(raw), 'after_identity': proof(after)})
    return mutations


class HandoffStore(ConditionalPageHistoryStore):
    def __init__(self, primary, mutations, s3=None):
        super().__init__(primary, s3)
        require(len(mutations) == 8 and {(m['bucket'], m['path']) for m in mutations}
                == {(ASSETS, path) for path in APPROVED}, 'handoff write roots differ')
        self.expected = {(m['bucket'], m['path']): m['after'] for m in mutations}

    def _require_root(self, bucket, path):
        require((bucket, path) in self.expected, 'handoff write outside approved roots')

    def put_if_match(self, bucket, path, raw, token):
        self._require_root(bucket, path)
        require(token is not None and raw == self.expected[bucket, path], 'unexpected handoff write')
        return super().put_if_match(bucket, path, raw, token)

    @staticmethod
    def _immutable_type(bucket, path, raw):
        require(bucket == ASSETS and re.fullmatch(re.escape(INTENTS) + r'[a-f0-9]{64}\.json', path),
                'handoff audit outside exact namespace')
        value = json.loads(raw)
        require(value.get('kind') == KIND and value.get('deletion_authorized') is False
                and publication.encode(value) == raw and path == INTENTS + proof(raw)['sha256'] + '.json',
                'handoff audit identity mismatch')
        require(len(value['mutations']) == 8 and {(m['bucket'], m['path']) for m in value['mutations']}
                == {(ASSETS, path) for path in APPROVED}, 'handoff audit roots differ')
        return 'application/json'


def apply(store, mutations, evidence, check_window, checkpoint, owner):
    """One CAS per record, with durable checkpoints and exact readback; no retry."""
    operation._window(check_window)
    tokens = []
    for m in mutations:
        raw, token = store.read_for_update(m['bucket'], m['path'])
        require(raw == m['before'] and token is not None, 'processing record changed before intent')
        tokens.append(token)
    intent = {'version': 1, 'kind': KIND, 'deletion_authorized': False, 'owner': owner,
              'evidence': evidence, 'mutations': [operation._packed(m) for m in mutations]}
    raw = publication.encode(intent)
    ref = {'bucket': ASSETS, 'path': INTENTS + proof(raw)['sha256'] + '.json', **proof(raw)}
    result = {'kind': 'reader-obsolete-pdf-processing-handoff-result', 'outcome': 'partial',
              'deletion_authorized': False, 'intent_ref': ref, 'intent_verified': False, 'owner': owner,
              'roots': [{'bucket': m['bucket'], 'path': m['path'], 'outcome': 'not_attempted', 'cas_attempts': 0}
                        for m in mutations]}
    checkpoint(copy.deepcopy(result))
    try:
        operation._window(check_window)
        store.immutable_put(ASSETS, ref['path'], raw)
        require(store.read_bytes(ASSETS, ref['path']) == raw, 'intent readback mismatch')
        result['intent_verified'] = True
    except Exception as error:
        result['error'] = 'intent: ' + type(error).__name__
        checkpoint(copy.deepcopy(result))
        return result
    for m, token, outcome in zip(mutations, tokens, result['roots']):
        try:
            current, latest = store.read_for_update(m['bucket'], m['path'])
            if current != m['before'] or latest != token:
                outcome['outcome'] = 'conflict'
                break
        except Exception as error:
            outcome.update(outcome='unknown', error=type(error).__name__)
            break
        outcome.update(outcome='unknown', cas_attempts=1)
        checkpoint(copy.deepcopy(result))
        try:
            require(store.read_bytes(ASSETS, ref['path']) == raw, 'intent changed after checkpoint')
            current, latest = store.read_for_update(m['bucket'], m['path'])
            require(current == m['before'] and latest == token, 'processing record changed after checkpoint')
            operation._window(check_window)
        except Exception as error:
            outcome.update(outcome='unknown', cas_attempts=0, error=type(error).__name__)
            break
        try:
            store.put_if_match(m['bucket'], m['path'], m['after'], token)
        except Exception as error:
            outcome['cas_error'] = type(error).__name__
        try:
            actual, _ = store.read_for_update(m['bucket'], m['path'])
            outcome['outcome'] = ('applied' if actual == m['after'] else
                                  'unknown' if actual == m['before'] else 'conflict')
        except Exception as error:
            outcome.update(outcome='unknown', error=type(error).__name__)
        checkpoint(copy.deepcopy(result))
        if outcome['outcome'] != 'applied':
            break
    if all(r['outcome'] == 'applied' for r in result['roots']):
        result['outcome'] = 'applied'
    checkpoint(copy.deepcopy(result))
    return result


def inspect(store, intent_ref):
    """Read-only recovery: report before/after/foreign/unknown, never resume."""
    raw = store.read_bytes(intent_ref['bucket'], intent_ref['path'])
    HandoffStore._immutable_type(intent_ref['bucket'], intent_ref['path'], raw)
    require(proof(raw) == {k: intent_ref[k] for k in ('bytes', 'sha256')}, 'handoff intent proof mismatch')
    roots = []
    for packed in json.loads(raw)['mutations']:
        m = operation._unpacked(packed)
        require(all(proof(m[k]) == m[k + '_identity'] for k in ('before', 'after')), 'mutation proof mismatch')
        row = {'bucket': m['bucket'], 'path': m['path']}
        try:
            actual = store.read_bytes(m['bucket'], m['path'])
            row['outcome'] = 'after' if actual == m['after'] else 'before' if actual == m['before'] else 'foreign'
        except Exception as error:
            row.update(outcome='unknown', error=type(error).__name__)
        roots.append(row)
    return {'roots': roots, 'resume_authorized': False, 'deletion_authorized': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'journal', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    raw = args.config.read_bytes()
    require(len(raw) <= 65000, 'handoff config exceeds 65000 bytes')
    config = json.loads(raw)
    validate_config(config)
    require(os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('GITHUB_REPOSITORY') == 'anftm/pipeline'
            and os.environ.get('GITHUB_WORKFLOW_REF') ==
                'anftm/pipeline/.github/workflows/reader-maintenance.yml@refs/heads/main'
            and os.environ.get('GITHUB_RUN_ATTEMPT') == '1'
            and re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', '')),
            'handoff requires the central maintenance first attempt')
    owner = {'repository': os.environ['GITHUB_REPOSITORY'], 'run_id': os.environ['GITHUB_RUN_ID'],
             'attempt': os.environ['GITHUB_RUN_ATTEMPT']}

    def isolated(state):
        reads, hub, fence = CollectionStore(), HubBucketStore(), GitHubProducerFence()
        fence.check()
        reader_gc_chm_window.require_clear(reads)
        result = {}
        args.output.parent.mkdir(parents=True, exist_ok=True)

        def checkpoint(outcome):
            result['operation'] = outcome
            state['obsolete_pdf_handoff'] = copy.deepcopy(result)
            maintenance.journal(args.journal, state)
            args.output.write_bytes(publication.encode(result))

        def metadata(bucket, path):
            rows = list(hub.api.get_bucket_paths_info(bucket, [path]))
            require(len(rows) == 1, 'PDF metadata absent or ambiguous')
            return {'bucket': bucket, 'path': path, 'size': rows[0].size, 'xet_hash': rows[0].xet_hash}

        with httpx.Client(timeout=120, follow_redirects=True) as client:
            evidence = observe(reads, client, config, metadata)
            mutations = prepare(reads, config, evidence)
            writes = HandoffStore(hub, mutations)

            def check_window():
                fence.check()
                require(publication.current(reads)[0] == evidence['pointer'], 'Reader generation changed')
                # Stream only the base catalog identity; never materialize its GBs.
                from .reader_gc_collect import catalog_identity
                require(catalog_identity(reads)['digest'] == evidence['base_catalog_sha256'],
                        'base catalog changed')
                reference_after(reads, config)
                return True

            outcome = apply(writes, mutations, evidence, check_window, checkpoint, owner)
            require(outcome['outcome'] == 'applied', 'partial handoff; inspect durable intent before any retry')
            fresh = observe(reads, client, config, metadata)
            require(operation._comparison(fresh) == operation._comparison(evidence), 'handoff evidence changed')
            after = inspect(writes, outcome['intent_ref'])
            require(all(r['outcome'] == 'after' for r in after['roots']), 'handoff collective readback mismatch')
            result['acceptance'] = {'roots': after, 'evidence': fresh}
            checkpoint(outcome)
            try:
                # The durable checkpoint is itself a boundary: verify the intent
                # and all eight roots again, then the window, before returning.
                require(inspect(writes, outcome['intent_ref']) == after,
                        'handoff changed after acceptance checkpoint')
                check_window()
            except Exception:
                result.pop('acceptance')
                checkpoint(outcome)
                raise
            return result

    maintenance.run(args.journal, isolated_callback=isolated,
                    suppress_resume_dispatch=True, preserve_queued_runs=True)


if __name__ == '__main__':
    main()
