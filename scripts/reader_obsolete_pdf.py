"""Read-only preparation for seven superseded font repairs and one CAJ copy.

The caller supplies fresh p.observe_consumer results for hf/pages, the projection
from p.project_sidecar(p.pinned_base(...), catalog), and exact file metadata.
No network client, migration, write or apply operation is provided here. Returned
bytes are review artifacts, not deletion authority; the caller owns live checks
and must revalidate roots/references before any later mutation.
"""
import copy
import gzip
import hashlib
import json
import re

from . import publish_reader_v3 as p, reader_page_audit as audit

BUCKET, INDEX = audit.BUCKET, audit.INDEX
SOURCE = ('key', 'repo', 'source_path', 'source_extension', 'source_revision', 'source_sha256')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def proof(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def remove_reference(raw, field, expected, *, array=False):
    """Delete one exact JSON member/array element, preserving every other byte."""
    text, decoder, matches = raw.decode('utf-8'), json.JSONDecoder(), []
    for match in re.finditer(r'"' + re.escape(field) + r'"\s*:\s*', text):
        value, end = decoder.raw_decode(text, match.end())
        if not array:
            if value == expected:
                matches.append((match.start(), end))
        elif isinstance(value, list):
            cursor = match.end() + 1
            for item in value:
                cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
                parsed, stop = decoder.raw_decode(text, cursor)
                if parsed == expected:
                    matches.append((cursor, stop))
                cursor = stop
                while text[cursor].isspace() or text[cursor] == ',':
                    cursor += 1
    require(len(matches) == 1, 'reference must occur exactly once: ' + field)
    start, end = matches[0]
    left, right = start - 1, end
    while text[left].isspace():
        left -= 1
    while text[right].isspace():
        right += 1
    if text[right] == ',':
        end = right + 1
    elif text[left] == ',':
        start = left
    result = (text[:start] + text[end:]).encode('utf-8')
    value = json.loads(raw)

    def strip(node):
        if isinstance(node, dict):
            if field in node:
                if array and isinstance(node[field], list):
                    node[field] = [item for item in node[field] if item != expected]
                elif node[field] == expected:
                    del node[field]
            for child in node.values():
                strip(child)
        elif isinstance(node, list):
            for child in node:
                strip(child)
    strip(value)
    require(json.loads(result) == value, 'reference removal changed unrelated values')
    return result


def prepare(store, *, inventory, exact_copies, manifest_inventory, ccrd_original,
            ccrd_root, observations, projection, metadata):
    """Return exact mutations, proofs and a bounded allowlist; never modify store.

    metadata(bucket, path) returns bucket_file.file_info-shaped data (size and
    xet_hash). PDF SHA-256 is independently calculated from store.read_bytes;
    an Xet hash is never treated as SHA-256. ccrd_root is (bucket, exact path).
    Inventories constrain scope, but never stand in for current store reads.
    A missing CCRD route yields a blocker and an empty deletion allowlist.
    """
    require(len(inventory) == len(exact_copies) == 8 and len(manifest_inventory) == 7,
            'expected exactly eight pairs and seven v2 manifests')
    copies = {row['old_manifest']: row for row in exact_copies}
    manifests = {row['path']: row['document'] for row in manifest_inventory}
    require(len(copies) == 8 and len(manifests) == 7, 'duplicate inventory paths')
    owners = [row for row in inventory if len(row['canonical_owners']) == 1]
    caj = [row for row in inventory if not row['canonical_owners']]
    require(len(owners) == 7 and len(caj) == 1, 'expected seven canonical owners and one CAJ')
    pointer, catalog = p.current(store)
    require(pointer and catalog.get('base_sidecar'), 'current pinned generation required')
    actual_projection = p.project_sidecar(p.pinned_base(store, catalog, {}), catalog)
    require(projection == actual_projection, 'injected pinned projection differs from current generation')
    require(set(observations) == {'hf', 'pages'}, 'both actual consumer observations required')
    for surface, receipt in observations.items():
        require(receipt.get('surface') == surface and receipt.get('active') is True
                and receipt.get('projection_verified') is True and receipt.get('checked_url')
                and receipt.get('checked_at') and receipt.get('generation') == pointer['generation']
                and receipt.get('catalog_sha256') == pointer['catalog']['sha256']
                and receipt.get('files') == len(catalog['files']),
                'consumer observation is missing or stale: ' + surface)
    roots = {}

    def read(bucket, path):
        raw = store.read_bytes(bucket, path)
        roots[bucket, path] = raw
        return raw

    def pdf(path, size, sha, xet=None):
        meta = metadata(BUCKET, path)
        require(meta and meta.get('bucket') == BUCKET and meta.get('path') == path
                and meta.get('size') == size and audit.SHA.fullmatch(str(meta.get('xet_hash', ''))),
                'PDF metadata mismatch: ' + path)
        require(xet is None or meta['xet_hash'] == xet, 'exact-copy Xet mismatch: ' + path)
        actual = proof(store.read_bytes(BUCKET, path))
        require(actual == {'bytes': size, 'sha256': sha}, 'PDF size/hash mismatch: ' + path)
        return {'bucket': BUCKET, 'path': path, **actual, 'metadata': copy.deepcopy(meta)}

    index_raw = read(BUCKET, INDEX)
    index = json.loads(index_raw)
    require(index.get('version') == 1 and index.get('kind') == 'pdf-derived-source-index', 'invalid index')
    entries = {row['key']: row for row in index['files']}
    require(len(entries) == len(index['files']), 'duplicate canonical keys')
    sidecar_raw = read(audit.ASSETS, audit.SIDECAR)
    canonical = json.loads(gzip.decompress(sidecar_raw))
    mutations, candidates, sources, replacements, blockers = [], [], [], [], []
    target_paths, after_index = [], index_raw

    def mutation(bucket, path, before, after, refs):
        mutations.append({'bucket': bucket, 'path': path, 'before': before, 'after': after,
                          'before_identity': proof(before), 'after_identity': proof(after),
                          'reference_mutations': refs})

    def route(key, path):
        expected = {'s': 2, 'm': 'p', 'p': path, 'b': BUCKET}
        return canonical['f'].get(key) == expected and projection['f'].get(key) == expected

    for row in inventory:
        old = row['manifest']
        audit.validate_entry(old)
        require(row['manifest_path'] == old['manifest_path'], 'legacy manifest path mismatch')
        raw = read(BUCKET, old['manifest_path'])
        require(json.loads(raw) == old, 'legacy manifest changed')
        exact = copies[old['manifest_path']]
        path = exact['new_pdf']
        match = audit.NEW.fullmatch(path)
        require(match and match[1] == match[2][:2] and match[2] == old['source_sha256']
                and exact['new_manifest'] == path.rsplit('/', 1)[0] + '/derived-manifest.json'
                and exact['old_pdf'] == old['new_path'] and exact['bytes'] == old['derived_bytes']
                and exact['sha256'] == old['derived_sha256'], 'exact-copy identity mismatch')
        copied_raw = read(BUCKET, exact['new_manifest'])
        require(audit.object_manifest_matches(old, path, json.loads(copied_raw)), 'copied manifest mismatch')
        old_proof = pdf(old['new_path'], exact['bytes'], exact['sha256'], exact['xet_hash'])
        copy_proof = pdf(path, exact['bytes'], exact['sha256'], exact['xet_hash'])
        pair = [old_proof, {'bucket': BUCKET, 'path': old['manifest_path'], **proof(raw)}]
        source = {field: old[field] for field in SOURCE}
        require(source['key'] == source['repo'] + '\0' + source['source_path'], 'source key mismatch')
        sources.append({**source, 'basis': 'matching manifest identities; original bytes not read'})
        if row['canonical_owners']:
            current = entries.get(old['key'])
            require(current == row['canonical_owners'][0], 'canonical owner changed')
            require(old['profile'] == 'gbk-font-repair-v1' and old['source_extension'] == 'pdf'
                    and current['profile'] == current['generation'] == 'gbk-font-repair-v2', 'repair profile mismatch')
            require(all(current.get(field) == source[field] for field in SOURCE), 'new source identity mismatch')
            new_path = current['new_path']
            new_match = audit.NEW.fullmatch(new_path)
            require(new_match and new_match[1] == new_match[2][:2] and new_match[2] == source['source_sha256']
                    and current['path'] == new_path and current['bucket'] == BUCKET
                    and current['manifest_path'] == new_path.rsplit('/', 1)[0] + '/derived-manifest.json'
                    and new_path != path, 'v2 object identity mismatch')
            require(route(old['key'], new_path), 'current route mismatch: ' + old['key'])
            manifest_raw = read(BUCKET, current['manifest_path'])
            manifest = json.loads(manifest_raw)
            require(manifest == manifests[current['manifest_path']]
                    and manifest == {**current, 'version': 1, 'kind': 'pdf-derived-source'}, 'v2 manifest identity mismatch')
            previous = {'bucket': BUCKET, 'path': old['new_path'], 'bytes': exact['bytes'],
                        'sha256': exact['sha256'], 'role': 'provenance'}
            require(current.get('previous_pdf') == previous, 'previous_pdf identity mismatch')
            replacements.append({'key': old['key'], 'pdf': pdf(new_path, current['derived_bytes'], current['derived_sha256']),
                                 'manifest': {'path': current['manifest_path'], **proof(manifest_raw)}})
            after_index = remove_reference(after_index, 'previous_pdf', previous)
            mutation(BUCKET, current['manifest_path'], manifest_raw,
                     remove_reference(manifest_raw, 'previous_pdf', previous),
                     [{'operation': 'remove', 'field': 'previous_pdf', 'before': previous}])
            pair += [copy_proof, {'bucket': BUCKET, 'path': exact['new_manifest'], **proof(copied_raw)}]
        else:
            require(old['profile'] == 'caj-family-pdf-v1' and old['source_extension'] == 'caj', 'unexpected eighth source')
            root_raw = read(*ccrd_root)
            root = json.loads(root_raw)
            expected_ref = ccrd_original['resources'][0]
            require(len(ccrd_original['resources']) == 1 and expected_ref == {
                'bucket': BUCKET, 'path': old['manifest_path'], **proof(raw), 'role': 'provenance'}, 'CCRD legacy resource mismatch')
            require(all(root.get(k) == v for k, v in ccrd_original.items() if k != 'resources')
                    and root['resources'].count(expected_ref) == 1, 'CCRD root identity mismatch')
            replacements.append({'key': old['key'], 'pdf': copy_proof,
                                 'manifest': {'path': exact['new_manifest'], **proof(copied_raw)}})
            if not route(old['key'], path):
                blockers.append({'key': old['key'], 'reason': 'CCRD current canonical/pinned route does not point to the identical active CAJ object',
                                 'canonical_route': canonical['f'].get(old['key']), 'pinned_route': projection['f'].get(old['key'])})
            else:
                mutation(*ccrd_root, root_raw, remove_reference(root_raw, 'resources', expected_ref, array=True),
                         [{'operation': 'remove', 'field': 'resources', 'before': expected_ref}])
        candidates.extend(pair)
        target_paths.extend(item['path'] for item in pair)
    require(len(set(target_paths)) == 30, 'expected exactly thirty distinct deletion targets')
    protected = {r[k]['path'] for r in replacements for k in ('pdf', 'manifest')}
    require(not set(target_paths) & protected, 'active object in deletion targets')
    for sidecar in (canonical, projection):
        for entry in sidecar['f'].values():
            for field, bucket_field in (('p', 'b'), ('pd', 'pdb'), ('o', 'ob'), ('c', 'cb'), ('f', 'fb')):
                require(not ((entry.get(bucket_field) or entry.get('b') or audit.ASSETS) == BUCKET
                             and entry.get(field) in target_paths), 'current route still references a deletion target')
    expected_index = copy.deepcopy(index)
    refs = []
    for entry in expected_index['files']:
        if entry['key'] in {row['manifest']['key'] for row in owners}:
            refs.append({'operation': 'remove', 'key': entry['key'], 'field': 'previous_pdf', 'before': entry.pop('previous_pdf')})
    require(json.loads(after_index) == expected_index, 'unrelated index field drift')
    mutation(BUCKET, INDEX, index_raw, after_index, refs)
    require(p.current(store)[0] == pointer and all(store.read_bytes(*key) == raw for key, raw in roots.items()),
            'inputs changed during preparation')
    return {'kind': 'reader-obsolete-pdf-preparation', 'bucket': BUCKET, 'applied': False, 'deletion_authorized': False,
            'blockers': blockers, 'mutations': mutations, 'target_delete_paths': sorted(target_paths),
            'delete_allowlist': [] if blockers else sorted(target_paths), 'candidates': candidates,
            'source_proofs': sources, 'replacement_proofs': replacements,
            'generation': pointer['generation'], 'observations': copy.deepcopy(observations),
            'projection_identity': proof(p.encode(projection)),
            'root_proofs': [{'bucket': b, 'path': path, **proof(raw)} for (b, path), raw in roots.items()]}
