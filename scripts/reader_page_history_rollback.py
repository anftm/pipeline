"""Read-only, single-hop historical catalog rollback resolution.

This verifies references, not permission or a procedure to publish a rollback.
Ordinary retirement without an alias keeps its existing behavior. Callers that
know a retired selection belongs to PDF replacement must set require_alias=True;
a PDF mapping alone cannot identify that selection once its catalog is gone.
No current pointer, ACK, promotion, retirement or object is written here.
"""
import copy
import json

try:
    from . import publish_reader_v3 as publication
    from . import reader_page_history_inventory as inventory
    from . import reader_page_redirects as redirects
except ImportError:
    import publish_reader_v3 as publication
    import reader_page_history_inventory as inventory
    import reader_page_redirects as redirects


def _generation(ref):
    publication.index_resource(ref)
    match = inventory.CATALOG_PATH.fullmatch(ref['path'])
    if not match:
        raise ValueError('unsupported rollback catalog path')
    return match[1]


def _selection(store, requested_ref, require_alias):
    generation = _generation(requested_ref)
    try:
        state = json.loads(store.read_bytes(publication.ASSETS, inventory.RETIREMENT))
    except FileNotFoundError:
        state = {'version': 1, 'kind': 'reader-v3-retirement', 'retired_paths': []}
    if (not isinstance(state, dict) or state.get('version') != 1
            or state.get('kind') != 'reader-v3-retirement'
            or not isinstance(state.get('retired_paths'), list)
            or any(not isinstance(path, str) for path in state['retired_paths'])
            or not isinstance(state.get('rollback_replacements', {}), dict)):
        raise ValueError('invalid retirement state')
    retired = set(state['retired_paths'])
    aliases = state.get('rollback_replacements', {})
    for original, ref in aliases.items():
        publication.text.sha(original)
        _generation(ref)
    standard = requested_ref['path'] == inventory.GENERATIONS + generation + '/catalog.json'
    target = None
    resolved = requested_ref
    if standard and requested_ref['path'] in retired:
        if generation in aliases:
            target, resolved = generation, aliases[generation]
        elif require_alias:
            raise ValueError('retired selected PDF catalog requires a rollback alias')
        else:
            return copy.deepcopy(requested_ref), None
    else:
        # Also accept an already-resolved PDF or CHM replacement, but only the
        # exact reference recorded by one original generation.
        owners = [original for original, ref in aliases.items()
                  if (ref['bucket'], ref['path']) == (requested_ref['bucket'], requested_ref['path'])]
        if owners:
            if len(owners) != 1 or aliases[owners[0]] != requested_ref:
                raise ValueError('ambiguous or conflicting rollback alias reference')
            target = owners[0]
        elif not standard:
            raise ValueError('replacement catalog requires a rollback alias')
    if target is not None:
        resolved_generation = _generation(resolved)
        owners = [original for original, ref in aliases.items()
                  if (ref['bucket'], ref['path']) == (resolved['bucket'], resolved['path'])]
        if owners != [target]:
            raise ValueError('ambiguous rollback alias target')
        if (resolved['path'] in retired or resolved_generation == target
                or resolved_generation in aliases):
            raise ValueError('retired, cyclic or multi-hop rollback alias target')
        if inventory.GENERATIONS + target + '/catalog.json' not in retired:
            raise ValueError('rollback alias original is not retired')
    return copy.deepcopy(resolved), target


def _validate(store, ref, target, mapping):
    catalog = publication.read_index(store, ref)
    if not isinstance(catalog, dict):
        raise ValueError('rollback catalog must be an object')
    publication.validate_catalog(catalog)
    if catalog['generation'] != _generation(ref):
        raise ValueError('rollback catalog path/generation mismatch')
    if target is not None and catalog.get('rollback_target') != target:
        raise ValueError('rollback_target differs from requested generation')
    if not catalog.get('base_sidecar'):
        raise ValueError('rollback catalog requires its own pinned base')
    base = publication.pinned_base(store, catalog, None)
    if mapping is not None:
        mapping = redirects.validated({redirects.FIELD: mapping})
        if any(part in {'.', '..'} for pair in mapping.items()
               for path in pair for part in path.split('/')):
            raise ValueError('dot segments are unsupported in PDF mapping paths')
        roots = sorted({old.rsplit('/', 1)[0] for old in mapping})
        for label, value in (('catalog', catalog), ('pinned base', base)):
            if next(inventory._selected(value, roots), None) is not None:
                raise ValueError('selected old PDF bundle remains in rollback ' + label)
    return catalog


def resolve_catalog_ref(store, requested_ref, *, mapping=None, require_alias=False):
    """Resolve only a retired standard reference, validating its one-hop alias.

    Non-alias standard references pass through without reading the catalog,
    including ordinary retired histories. require_alias rejects that latter
    case for a caller-selected PDF history. Replacement refs are always checked.
    Only read_bytes is required; the missing original catalog is never read.
    """
    resolved, target = _selection(store, requested_ref, require_alias)
    if target is not None:
        _validate(store, resolved, target, mapping)
    return resolved


def verify(store, requested_ref, *, mapping=None, require_alias=False):
    """Verify a resolved catalog and pinned base for a future read-only CLI.

    Unlike resolve_catalog_ref's ordinary pass-through, this reads even an
    unchanged catalog. A successful report does not authorize publication.
    """
    resolved, target = _selection(store, requested_ref, require_alias)
    catalog = _validate(store, resolved, target, mapping)
    return {'version': 1, 'kind': 'reader-page-history-rollback-verification',
            'requested_catalog': copy.deepcopy(requested_ref), 'catalog_ref': resolved,
            'generation': catalog['generation'], 'rollback_target': target,
            'base_sidecar': copy.deepcopy(catalog['base_sidecar']),
            'alias_used': target is not None, 'resolved': resolved != requested_ref,
            'selected_bundles_checked': mapping is not None, 'verified': True,
            'applied': False, 'publication_authorized': False}
