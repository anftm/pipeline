"""Read-only PDF history inventory; neither a rewrite plan nor a full GC graph.

Only read_bytes/list_files are required. Catalogs are discovered from promotions,
retirement resources/rollback aliases, the current pointer and listed generation
catalogs (including CHM sanitized histories without promotions). Legacy generation
references are reported independently. No copy equivalence or deletion is proven.
"""
import copy
import json
import re

try:
    from . import publish_reader_v3 as publication, reader_page_audit as audit
    from . import reader_page_migrate as migration, reader_page_redirects as redirects
except ImportError:
    import publish_reader_v3 as publication
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_redirects as redirects

PROMOTIONS = 'reader-index/v3/promotions/'
GENERATIONS = 'reader-index/v3/generations/'
RETIREMENT = 'reader-index/v3/retirement.json'
CATALOG_PATH = re.compile(
    r'reader-index/v3/(?:generations/|history-replacements/derived-pdf/[0-9a-f]{64}/)'
    r'([0-9a-f]{64})/catalog\.json\Z')


def _child(location, key):
    return location + '/' + str(key).replace('~', '~0').replace('/', '~1')


def _selected(value, roots, location=''):
    """Exact JSON-pointer locations, including keys and unknown metadata."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _selected(key, roots, _child(location, key) + '/@key')
            yield from _selected(child, roots, _child(location, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _selected(child, roots, _child(location, index))
    elif isinstance(value, str):
        for root in roots:
            if value == root or value.startswith(root + '/'):
                yield {'location': location, 'path': value, 'bundle_root': root}
                break


def inventory(store, mapping):
    """Return deterministic classifications and exact blockers without mutation.

    mapping is the caller-selected old PDF -> new PDF scope, not an audit proof.
    Missing optional pointer/retirement roots are recorded. A missing legacy
    catalog, unsupported reference, checksum conflict or unreadable input blocks
    scope completeness. Even a complete scope never establishes unreferencedness.
    """
    mapping = copy.deepcopy(redirects.validated({redirects.FIELD: mapping}))
    if not mapping:
        raise ValueError('a scoped PDF mapping is required')
    if any(part in {'.', '..'} for pair in mapping.items() for path in pair for part in path.split('/')):
        raise ValueError('dot segments are unsupported in PDF mapping paths')
    roots = sorted({old.rsplit('/', 1)[0] for old in mapping})
    blockers, evidence, catalogs, aliases, legacy = [], {}, {}, [], []
    root_state = {}

    def block(code, source, location='', **details):
        blockers.append({'code': code, 'source': source, 'location': location, **details})

    def remember(ref, source, location):
        key = (ref['bucket'], ref['path'])
        prior = evidence.get(key)
        if prior and (prior['sha256'], prior['bytes']) != (ref['sha256'], ref['bytes']):
            block('digest_conflict', source, location, reference=copy.deepcopy(ref),
                  previous_reference=copy.deepcopy(prior))
        else:
            evidence[key] = copy.deepcopy(ref)

    def document(path, *, optional=False):
        try:
            raw = store.read_bytes(audit.ASSETS, path)
        except FileNotFoundError as exc:
            root_state[path] = 'absent'
            if not optional:
                block('missing_document', path, error=str(exc))
            return None
        except (ValueError, OSError) as exc:
            block('unreadable_document', path, error=str(exc))
            return None
        ref = publication.metadata(audit.ASSETS, path, raw)
        remember(ref, path, '')
        root_state[path] = 'observed'
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('expected an object')
            return value
        except (ValueError, UnicodeError) as exc:
            block('invalid_document', path, error=str(exc))
            return None

    def add_catalog(ref, source, location, expected_generation=None, alias=None):
        try:
            publication.index_resource(ref)
            match = CATALOG_PATH.fullmatch(ref['path'])
            if not match:
                raise ValueError('unsupported catalog path')
            if expected_generation is not None and match[1] != expected_generation:
                raise ValueError('catalog path differs from referring generation')
        except (ValueError, TypeError, KeyError) as exc:
            block('unsupported_reference', source, location, reference=copy.deepcopy(ref), error=str(exc))
            return
        remember(ref, source, location)
        key = (ref['bucket'], ref['path'])
        item = catalogs.setdefault(key, {'catalog_ref': copy.deepcopy(ref),
            'generation': match[1], 'sources': [], 'aliases': []})
        origin = {'source': source, 'location': location}
        if origin not in item['sources']:
            item['sources'].append(origin)
        if alias is not None:
            item['aliases'].append(alias)

    def unsupported(value, source, location=''):
        for hit in _selected(value, roots, location):
            block('unsupported_selected_reference', source, **hit)

    pointer = document(publication.POINTER, optional=True)
    current_generation = None
    if pointer is not None:
        if (pointer.get('version') != 1 or pointer.get('kind') != 'reader-reading-pointer'
                or not audit.SHA.fullmatch(str(pointer.get('generation', '')))
                or not isinstance(pointer.get('resources', []), list)):
            block('invalid_pointer', publication.POINTER)
        else:
            current_generation = pointer['generation']
            add_catalog(pointer.get('catalog'), publication.POINTER, '/catalog', current_generation)
            for i, ref in enumerate(pointer.get('resources', [])):
                add_catalog(ref, publication.POINTER, f'/resources/{i}')
        unsupported(pointer, publication.POINTER)

    for prefix in (PROMOTIONS, GENERATIONS):
        try:
            paths = sorted(set(store.list_files(audit.ASSETS, (prefix,))))
        except (ValueError, OSError) as exc:
            block('listing_failed', prefix, error=str(exc))
            continue
        for path in paths:
            if prefix == GENERATIONS:
                if not path.endswith('/catalog.json'):
                    continue
                value = document(path)
                if value is not None:
                    add_catalog(evidence[audit.ASSETS, path], path, '')
                continue
            generation = path.removeprefix(PROMOTIONS).removesuffix('.json')
            if not audit.SHA.fullmatch(generation) or path != PROMOTIONS + generation + '.json':
                block('unsupported_promotion_path', path)
                continue
            promotion = document(path)
            if promotion is None:
                continue
            if (promotion.get('version') != 1 or promotion.get('kind') != 'reader-reading-promotion'
                    or promotion.get('generation') != generation
                    or not isinstance(promotion.get('resources'), list) or not promotion['resources']):
                block('invalid_promotion', path)
                continue
            for i, ref in enumerate(promotion['resources']):
                add_catalog(ref, path, f'/resources/{i}', generation)
            unsupported(promotion, path)

    retirement = document(RETIREMENT, optional=True)
    if retirement is not None:
        if (retirement.get('version') != 1 or retirement.get('kind') != 'reader-v3-retirement'
                or not isinstance(retirement.get('resources'), list)
                or not isinstance(retirement.get('retired_paths'), list)
                or not isinstance(retirement.get('rollback_replacements', {}), dict)):
            block('invalid_retirement', RETIREMENT)
        else:
            for i, ref in enumerate(retirement['resources']):
                add_catalog(ref, RETIREMENT, f'/resources/{i}')
            for generation, ref in sorted(retirement.get('rollback_replacements', {}).items()):
                location = _child('/rollback_replacements', generation)
                aliases.append({'generation': generation, 'reference': copy.deepcopy(ref),
                                'source': RETIREMENT, 'location': location, 'must_preserve': True})
                if not audit.SHA.fullmatch(generation):
                    block('invalid_alias_generation', RETIREMENT, location, reference=copy.deepcopy(ref))
                add_catalog(ref, RETIREMENT, location, alias=generation)
        unsupported(retirement, RETIREMENT)

    for key, item in sorted(catalogs.items()):
        source = item['catalog_ref']['path']
        before = len(blockers)
        item.update(is_current=item['generation'] == current_generation,
                    classification='blocked', selected_references=[])
        try:
            catalog = publication.read_index(store, item['catalog_ref'])
            if not isinstance(catalog, dict):
                raise ValueError('catalog must be an object')
            publication.validate_catalog(catalog)
            if catalog['generation'] != item['generation']:
                raise ValueError('catalog path/generation mismatch')
            if not catalog.get('base_sidecar'):
                raise ValueError('catalog requires its own pinned base')
            remember(catalog['base_sidecar'], source, '/base_sidecar')
            base = publication.pinned_base(store, catalog, None)
            item['base_sidecar'] = copy.deepcopy(catalog['base_sidecar'])
            item['rollback_target'] = catalog.get('rollback_target')
            for generation in item['aliases']:
                if catalog.get('rollback_target') != generation:
                    block('alias_target_mismatch', source, '/rollback_target', expected=generation,
                          actual=catalog.get('rollback_target'))
            supported = set()
            for route, entry in base['f'].items():
                if not isinstance(entry, dict):
                    raise ValueError('invalid pinned sidecar entry')
                default = entry.get('b') or audit.ASSETS
                for field, qualifier in redirects.PATH_FIELDS:
                    path = entry.get(field)
                    if isinstance(path, str) and path in mapping and (entry.get(qualifier) or default) == audit.BUCKET:
                        supported.add(_child(_child('/f', route), field))
            for hit in _selected(base, roots):
                item['selected_references'].append({'source': catalog['base_sidecar']['path'], **hit})
                if hit['location'] not in supported:
                    block('unsupported_selected_reference', catalog['base_sidecar']['path'], **hit)
            selected_redirects = set(redirects.validated(catalog)) & set(mapping)
            for old in sorted(selected_redirects):
                if catalog[redirects.FIELD][old] != mapping[old]:
                    block('redirect_conflict', source, _child('/' + redirects.FIELD, old),
                          expected=mapping[old], actual=catalog[redirects.FIELD][old])
            for hit in _selected(catalog, roots):
                item['selected_references'].append({'source': source, **hit})
                if not any(hit['location'] == _child('/' + redirects.FIELD, old) + '/@key'
                           for old in selected_redirects):
                    block('unsupported_selected_reference', source, **hit)
            item['needs_rewrite'] = bool(item['selected_references'])
            item['classification'] = ('blocked' if len(blockers) != before else
                'needs_rewrite' if item['needs_rewrite'] else 'no_selected_references_in_catalog_and_base')
        except (ValueError, TypeError, KeyError, AttributeError, OSError, EOFError) as exc:
            block('invalid_catalog_or_base', source, reference=item['catalog_ref'], error=str(exc))

    legacy_catalog = document(migration.CATALOG)
    if legacy_catalog is not None:
        if (legacy_catalog.get('version') != 1 or legacy_catalog.get('kind') != 'reader-catalog'
                or not isinstance(legacy_catalog.get('generations'), dict)):
            block('invalid_legacy_catalog', migration.CATALOG)
        else:
            for generation, entry in sorted(legacy_catalog['generations'].items()):
                location = _child('/generations', generation)
                if not isinstance(entry, dict) or not isinstance(entry.get('references'), list):
                    block('invalid_legacy_generation', migration.CATALOG, location)
                    continue
                hits = []
                for i, ref in enumerate(entry['references']):
                    ref_location = _child(location + '/references', i)
                    if (not isinstance(ref, dict) or not isinstance(ref.get('bucket'), str)
                            or not isinstance(ref.get('path'), str)):
                        block('unsupported_reference', migration.CATALOG, ref_location, reference=ref)
                        continue
                    for hit in _selected(ref, roots, ref_location):
                        hits.append({'reference': copy.deepcopy(ref), **hit})
                        if hit['location'] != ref_location + '/path' or ref['bucket'] != audit.BUCKET:
                            block('unsupported_selected_reference', migration.CATALOG, **hit)
                # Preserve every reference and checksum, including unrelated PDFs.
                legacy.append({'generation': generation, 'status': entry.get('status'),
                    'entry': copy.deepcopy(entry), 'selected_root_references': hits})
                unsupported({k: v for k, v in entry.items() if k != 'references'}, migration.CATALOG, location)
            unsupported({k: v for k, v in legacy_catalog.items() if k != 'generations'}, migration.CATALOG)

    # Bind observations to their exact bytes; this is still not a serialized graph.
    for key, ref in sorted(evidence.items()):
        try:
            raw = store.read_bytes(*key)
            actual = publication.metadata(*key, raw)
            if (actual['sha256'], actual['bytes']) != (ref['sha256'], ref['bytes']):
                block('evidence_changed', ref['path'], reference=ref, observed=actual)
        except (ValueError, OSError) as exc:
            block('evidence_unreadable', ref['path'], reference=ref, error=str(exc))
    for item in catalogs.values():
        refs = [item['catalog_ref']]
        if 'base_sidecar' in item:
            refs.append(item['base_sidecar'])
        identities = {(ref['bucket'], ref['path']) for ref in refs}
        if any(b['code'] in {'digest_conflict', 'evidence_changed', 'evidence_unreadable'} and
               (b['reference']['bucket'], b['reference']['path']) in identities for b in blockers):
            item['classification'] = 'blocked'
    return {'version': 1, 'kind': 'reader-derived-pdf-history-inventory',
        'mapping': mapping, 'selected_old_bundles': roots,
        'discovery_scope': 'promotions-retirement-aliases-listed-catalogs-current-legacy-generations',
        'root_observations': root_state, 'histories': [catalogs[key] for key in sorted(catalogs)],
        'aliases_needing_preserve': aliases, 'legacy_generations': legacy,
        'evidence_resources': [evidence[key] for key in sorted(evidence)], 'blockers': blockers,
        'scope_complete': not blockers, 'graph_complete': False, 'fully_unreferenced': False,
        'apply_allowed': False, 'deletion_authorized': False, 'remote_mutations': 0}
