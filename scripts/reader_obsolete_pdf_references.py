"""Read-only incoming-reference proof for seven in-place v2 manifest edits.

Call fresh() again inside the writer window immediately before apply. This proof
is neither mutation authority nor a lock: later writers can invalidate it. GC
retained audit intents are not runtime roots; their original before bytes and
hash bindings must remain intact, without a live update.
"""
import copy
import json
import posixpath

from . import reader_gc_graph as graph, reader_obsolete_pdf as preparation

BUCKET, INDEX = preparation.BUCKET, preparation.INDEX
require, proof = preparation.require, preparation.proof


def _targets(mutations):
    index = [m for m in mutations if (m['bucket'], m['path']) == (BUCKET, INDEX)]
    targets = [m for m in mutations if m['bucket'] == BUCKET
               and m['path'].endswith('/derived-manifest.json')]
    require(len(index) == 1 and len(targets) == 7
            and len({m['path'] for m in targets}) == 7, 'expected planned index and seven manifests')
    index = index[0]
    entries = json.loads(index['before'])['files']
    after_index = index['before']
    for m in targets:
        rows = [row for row in entries if row.get('manifest_path') == m['path']]
        require(len(rows) == 1, 'manifest lacks unique canonical owner')
        row = rows[0]
        require(row.get('bucket') == BUCKET and row.get('profile') == row.get('generation') == 'gbk-font-repair-v2'
                and json.loads(m['before']) == {**row, 'version': 1, 'kind': 'pdf-derived-source'},
                'planned manifest differs from canonical v2 owner')
        require(m['after'] == preparation.remove_reference(m['before'], 'previous_pdf', row['previous_pdf']),
                'unexpected manifest edit')
        after_index = preparation.remove_reference(after_index, 'previous_pdf', row['previous_pdf'])
    require(index['after'] == after_index, 'unexpected canonical index edit')
    for m in [index, *targets]:
        require(m['before_identity'] == proof(m['before']), 'planned before identity mismatch')
    return index, targets


def _bindings(value, target, origin, files):
    """Conservatively inspect hashes on paths that could resolve to a known edge.

    Use the graph's path vocabulary and inventory, including inherited bucket
    context and bundle-relative paths. PDF derived_* identities are not manifest
    hashes. Unknown qualifiers must not hide a possible incoming binding.
    """
    found = []

    def walk(node, bucket, explicit=False, field=''):
        matches = set()
        if isinstance(node, dict):
            if 'bucket' in node:
                bucket, explicit = node['bucket'], True
            for key, child in node.items():
                selected = node.get(graph.BUCKET_FIELDS.get(key, ''))
                if key == 'o' and not isinstance(selected, str):
                    selected = node.get('obucket')
                    if not selected and origin[1].endswith('reader_assets.json.gz'):
                        selected = node.get('b')
                if isinstance(selected, str):
                    matches.update(walk(child, selected, True, key))
                else:
                    matches.update(walk(child, bucket, explicit, key))
            binding = {name: node[name] for name in ('sha256', 'bytes', 'manifest_sha256', 'manifest_bytes')
                       if name in node}
            if binding:
                for matched_field in sorted(matches):
                    found.append({'field': matched_field, **binding,
                                  **({'role': node['role']} if 'role' in node else {})})
        elif isinstance(node, list):
            for child in node:
                matches.update(walk(child, bucket, explicit, field))
        elif isinstance(node, str) and field in graph.PATH_FIELDS | {'references'}:
            paths = {node}
            if graph.is_manifest(origin[1]):
                paths.add(posixpath.normpath(posixpath.join(posixpath.dirname(origin[1]), node)))
            path_matches = target[1] in paths or (field == 'root' and any(
                target[1].startswith(path.rstrip('/') + '/') for path in paths))
            known_bucket = isinstance(bucket, str) and bucket in files
            bucket_matches = (bucket == target[0] if explicit and known_bucket else
                              target[1] in files.get(target[0], set()))
            if path_matches and bucket_matches:
                matches.add(field)
        return matches

    walk(value, origin[0])
    return found


def inspect_graph(reference, report, mutations):
    """Inspect actual ReferenceGraph state, never just a supplied completeness flag.

    Return exact before identities, incoming origins, expected bindings and root
    digests. Any expected_resources entry for a target blocks, including advisory
    provenance. Reads are checked against loaded digests; no store writes occur.
    """
    index, targets = _targets(mutations)
    result = {'kind': 'reader-obsolete-pdf-reference-proof', 'graph_complete': False,
              'blockers': [], 'targets': [], 'root_digests': [], 'readonly_domains': list(graph.BUCKETS),
              'incoming_bindings_absent': False,
              'planned_index': {'bucket': BUCKET, 'path': INDEX, 'before_identity': proof(index['before'])},
              'mutation_authorized': False, 'deletion_authorized': False,
              'audit_policy': 'Retained GC intents are not traversed runtime roots; preserve original before bytes and hash bindings.'}
    blockers = result['blockers']
    if type(reference) is not graph.ReferenceGraph or not isinstance(report, dict):
        blockers.append('actual ReferenceGraph and complete build report required')
        return result
    if (report.get('graph_complete') is not True or report.get('blockers') != []
            or reference.blockers or reference.unreadable or reference.pending or reference.missing
            or report.get('workers_may_still_be_running')):
        blockers.append('reference graph is incomplete, unreadable or blocked')
        return result
    if (set(reference.files) != set(graph.BUCKETS) or set(report.get('buckets', {})) != set(graph.BUCKETS)
            or not reference.include_input):
        blockers.append('all four reference domains must be inventoried')
        return result
    required = (preparation.audit.ASSETS, preparation.audit.SIDECAR)
    if required not in reference.root_hashes or (BUCKET, INDEX) not in reference.root_hashes:
        blockers.append('required canonical roots were not traversed')
        return result
    result['graph_complete'] = True
    loaded = {}

    def read(ref):
        if ref in loaded:
            return loaded[ref]
        if (ref[1] not in reference.files.get(ref[0], set()) or ref not in reference.direct
                or ref not in reference.loaded_digests):
            blockers.append('unscanned reference: ' + ':'.join(ref))
            return None
        try:
            raw = reference.store.read_bytes(*ref)
            require(proof(raw)['sha256'] == reference.loaded_digests[ref], 'loaded digest changed')
            loaded[ref] = raw
            return raw
        except Exception as error:
            blockers.append('reference unreadable or changed: ' + ':'.join(ref) + ' (' + type(error).__name__ + ')')
            return None

    for ref, digest in sorted(reference.root_hashes.items()):
        if reference.loaded_digests.get(ref) != digest:
            blockers.append('root digest differs from loaded digest: ' + ':'.join(ref))
        result['root_digests'].append({'bucket': ref[0], 'path': ref[1], 'sha256': digest})
    if read((BUCKET, INDEX)) != index['before']:
        blockers.append('canonical index differs from planned before bytes')
    for m in targets:
        ref = (m['bucket'], m['path'])
        item = {'bucket': ref[0], 'path': ref[1], 'before_identity': proof(m['before']),
                'incoming_origins': [], 'expected_resources': []}
        result['targets'].append(item)
        if read(ref) != m['before']:
            blockers.append('manifest unscanned or differs from planned before bytes: ' + ref[1])
        if ref in reference.expected_resources:
            item['expected_resources'].append(copy.deepcopy(reference.expected_resources[ref]))
            blockers.append('expected resource binding blocks manifest edit: ' + ref[1])
        for origin, refs in sorted(reference.direct.items()):
            if ref not in refs:
                continue
            raw = read(origin)
            evidence = {'bucket': origin[0], 'path': origin[1],
                        'sha256': reference.loaded_digests.get(origin), 'bindings': []}
            item['incoming_origins'].append(evidence)
            if raw is None:
                continue
            try:
                evidence['bindings'] = _bindings(graph.decode(raw), ref, origin, reference.files)
            except Exception as error:
                blockers.append('incoming origin cannot be inspected: ' + ':'.join(origin) + ' (' + type(error).__name__ + ')')
                continue
            # A manifest's own manifest_path is an identity, not an incoming hash.
            if origin == ref:
                evidence['relation'] = 'manifest-self'
            elif evidence['bindings']:
                blockers.append('incoming hash/bytes binding blocks manifest edit: ' + ':'.join(origin))
            elif origin == (BUCKET, INDEX) and raw == index['before']:
                evidence['relation'] = 'planned-canonical-index-without-hash-binding'
            else:
                evidence['relation'] = 'qualified-reference-without-hash-binding'
    result['incoming_bindings_absent'] = not blockers
    return result


class ReferenceProofBlocked(ValueError):
    """A failed gate, with a JSON-serializable diagnostic proof attached."""

    def __init__(self, evidence):
        self.proof = evidence
        super().__init__('incoming manifest reference proof blocked: ' + '; '.join(evidence['blockers']))


def verify(reference, report, plan):
    """Fail-closed runner gate alongside fresh root/document barrier checks.

    This checks before-in-place bindings, not post-retirement deletion safety.
    Returns JSON-serializable evidence only on success; blocked evidence is
    available as ReferenceProofBlocked.proof and cannot silently permit apply.
    """
    evidence = inspect_graph(reference, report, plan['mutations'])
    if evidence['blockers'] or not evidence['incoming_bindings_absent']:
        raise ReferenceProofBlocked(evidence)
    return evidence


def fresh(store, plan):
    """Build all four read-only domains now and inspect the seven exact edits."""
    reference = graph.ReferenceGraph(store, include_input=True)
    return verify(reference, reference.build(), plan)
