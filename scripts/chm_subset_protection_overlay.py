"""Read-only hypothetical independent protection root for subset diagnostics.

The view never publishes its synthetic root. No workflow lock, durable root,
storage writer, delete or producer restore is provided by this module. A graph
result from this view remains hypothetical even when it reads real objects.
"""
try:
    from . import chm_subset_isolated_root as isolated, reader_gc_graph as graph, reader_gc_stream as stream
except ImportError:
    import chm_subset_isolated_root as isolated
    import reader_gc_graph as graph
    import reader_gc_stream as stream


def view(store, withheld, identity):
    candidate = isolated.build(withheld, identity)
    root, raw = candidate['path'], candidate['raw']
    if not graph.is_root(root, isolated.subset.ASSETS):
        raise ValueError('hypothetical CHM protection is not a graph root')
    existing = store.list_files(isolated.subset.ASSETS, (root,))
    if root in existing:
        raise ValueError('hypothetical protection path already exists')

    class ReadOnlyView:
        def __init__(self):
            self.object_sizes = dict(getattr(store, 'object_sizes', {}))
            self.object_sizes[(isolated.subset.ASSETS, root)] = len(raw)
            self.read_etags = {}
            self.root_etags = {}

        def _remember(self, bucket, path):
            ref = (bucket, path)
            tag = getattr(store, 'read_etags', {}).get(ref)
            if tag is None:
                self.read_etags.pop(ref, None)
            else:
                self.read_etags[ref] = tag

        def list_files(self, bucket, prefixes):
            current = set(store.list_files(bucket, prefixes))
            if bucket == isolated.subset.ASSETS and root.startswith(prefixes):
                if root in current:
                    raise ValueError('hypothetical protection path appeared during scan')
                current.add(root)
            return current

        def list_roots(self, bucket, parents):
            current = {path: tag for path, tag in store.list_roots(bucket, parents).items()
                       if graph.is_root(path, bucket)}
            if bucket == isolated.subset.ASSETS:
                if root in current:
                    raise ValueError('hypothetical protection path appeared during root scan')
                current[root] = None
            self.root_etags[bucket] = current
            return current

        def read_bytes(self, bucket, path):
            if (bucket, path) == (isolated.subset.ASSETS, root):
                return raw
            result = store.read_bytes(bucket, path)
            self._remember(bucket, path)
            return result

        def read_graph_json(self, bucket, path, parser=None):
            if (bucket, path) == (isolated.subset.ASSETS, root):
                return (parser or stream.graph_json)([raw], False)
            if callable(getattr(store, 'read_graph_json', None)):
                result = store.read_graph_json(bucket, path, parser=parser)
                self._remember(bucket, path)
                return result
            result = self.read_bytes(bucket, path)
            return (parser or stream.graph_json)([result], path.endswith('.gz'))

        def digest(self, bucket, path):
            if (bucket, path) == (isolated.subset.ASSETS, root):
                return candidate['sha256']
            if callable(getattr(store, 'digest', None)):
                return store.digest(bucket, path)
            return isolated.subset.chm.digest(self.read_bytes(bucket, path))

    return ReadOnlyView(), candidate


def capture(traversal, report, rows, selected, withheld, *, candidate, expected_absent):
    """Capture target reachability from the synthetic root, never authorize it."""
    if (not isinstance(report, dict) or report.get('graph_complete') is not True
            or report.get('blockers') != [] or report.get('missing') != []
            or report.get('ambiguous_paths') != [] or report.get('deletion_enabled') is not False
            or set(traversal.files) != set(graph.BUCKETS)):
        raise ValueError('hypothetical independent-root graph is incomplete')
    if (not isinstance(candidate, dict) or not isinstance(candidate.get('raw'), bytes)
            or not isinstance(candidate.get('path'), str)
            or isolated.subset.chm.digest(candidate['raw']) != candidate.get('sha256')):
        raise ValueError('hypothetical root candidate identity differs')
    expected_path = candidate['path']
    if (not isolated.ROOT.fullmatch(expected_path) or not isinstance(rows, list)
            or not isinstance(selected, list) or not isinstance(withheld, list)):
        raise ValueError('hypothetical root candidate path differs')
    target_map = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('bucket') != isolated.subset.ASSETS:
            raise ValueError('invalid diagnostic inventory')
        path = row.get('path')
        if not isinstance(path, str) or not isolated.subset.TARGET.fullmatch(path) or path in target_map:
            raise ValueError('invalid or duplicate diagnostic target')
        target_map[path] = row
    groups = []
    for group in (selected, withheld):
        refs = set()
        for row in group:
            path = row.get('path') if isinstance(row, dict) else None
            if path not in target_map or row != target_map[path] or path in refs:
                raise ValueError('diagnostic scope differs from inventory')
            refs.add(path)
        groups.append(refs)
    chosen, kept = groups
    if not chosen or not kept or chosen & kept:
        raise ValueError('diagnostic scopes are empty or overlap')
    all_refs = {(isolated.subset.ASSETS, path) for path in target_map}
    absent = set(expected_absent)
    actual_missing = {(isolated.subset.ASSETS, path) for path in target_map
                      if path not in traversal.files[isolated.subset.ASSETS]}
    if absent != actual_missing or not absent <= all_refs or absent & {
            (isolated.subset.ASSETS, path) for path in chosen | kept}:
        raise ValueError('diagnostic target presence differs from saved receipts')
    retained = all_refs & traversal.references
    if retained != {(isolated.subset.ASSETS, path) for path in kept}:
        raise ValueError('hypothetical graph does not protect exactly the withheld targets')
    root = (isolated.subset.ASSETS, candidate['path'])
    members = {(bucket, path) for bucket, files in traversal.files.items()
               for path in files if graph.is_root(path, bucket)}
    roots, closure, retired = traversal.root_hashes, traversal.loaded_digests, traversal.retired_roots
    if (root not in members or root in retired or roots.get(root) != candidate['sha256']
            or closure.get(root) != candidate['sha256'] or not (members - retired) <= set(roots)
            or not set(roots) <= members or any(closure.get(ref) != sha for ref, sha in roots.items())):
        raise ValueError('hypothetical root or complete closure identity differs')
    return {'mode': 'hypothetical-overlay', 'graph_complete': True, 'blockers': [], 'ambiguous_paths': [],
            'target_count': len(target_map), 'retained_target_paths': [
                {'bucket': bucket, 'path': path} for bucket, path in sorted(retained)],
            'root_members': [{'bucket': bucket, 'path': path} for bucket, path in sorted(members)],
            'root_sha256': [{'bucket': bucket, 'path': path, 'sha256': sha}
                            for (bucket, path), sha in sorted(roots.items())],
            'retired_roots': [{'bucket': bucket, 'path': path} for bucket, path in sorted(retired)],
            'closure_sha256': [{'bucket': bucket, 'path': path, 'sha256': sha}
                               for (bucket, path), sha in sorted(closure.items())],
            'root_recheck_complete': False, 'reader_acceptance_complete': False,
            'remote_mutations': 0, 'deletion_authorized': False}
