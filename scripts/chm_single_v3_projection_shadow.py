"""Pure simulation of the interrupted CHM's v3 projection and receipts.

This is not a publication or recovery implementation. It does not read from
or write to a store, contact consumers, adapt withdrawal receipts, acknowledge
anything, or establish writer isolation. Synthetic receipt success is never a
real consumer acceptance or permission to mutate.
"""
import copy
import gzip
import hashlib
import io
import json

try:
    from . import chm_single_interrupted_preflight as fixed, publish_reader_v3 as publication
except ImportError:
    import chm_single_interrupted_preflight as fixed
    import publish_reader_v3 as publication


RESET_ID = 'document-rebuild-20261025'
MAX_SIDECAR_BYTES = 50 * 1024 * 1024


def _reset_identity(pointer, catalog, receipt):
    if (not isinstance(receipt, dict) or receipt.get('version') != 1
            or receipt.get('kind') != 'reader-document-reset'
            or receipt.get('status') != 'withdrawn' or receipt.get('reset_id') != RESET_ID
            or catalog.get('document_reset') != receipt.get('reset_id')
            or receipt.get('generation') != pointer['generation']
            or receipt.get('base_snapshot') != catalog.get('base_sidecar') or catalog.get('files')):
        raise ValueError('document reset receipt does not bind the current generation')


def simulate(pointer, catalog, old_base_raw, new_sidecar, receipt):
    """Produce deterministic in-memory projection evidence, never a write plan."""
    if not isinstance(pointer, dict) or pointer.get('generation') != fixed.PARENT_V3:
        raise ValueError('v3 parent changed')
    if (pointer.get('version') != 1 or pointer.get('kind') != 'reader-reading-pointer'
            or not isinstance(catalog, dict)):
        raise ValueError('invalid v3 parent identity')
    publication.validate_catalog(catalog)
    if catalog['generation'] != pointer['generation']:
        raise ValueError('v3 parent catalog differs')
    old_raw = publication.encode(catalog)
    old_ref = publication.metadata(fixed.ASSETS,
        f"reader-index/v3/generations/{pointer['generation']}/catalog.json", old_raw)
    if pointer.get('catalog') != old_ref or pointer.get('resources') != [old_ref]:
        raise ValueError('v3 parent pointer differs')
    _reset_identity(pointer, catalog, receipt)
    if type(old_base_raw) is not bytes or hashlib.sha256(old_base_raw).hexdigest() != fixed.SIDECAR_SHA256:
        raise ValueError('original canonical sidecar bytes differ')
    old_base_ref = catalog.get('base_sidecar')
    publication.base_sidecar_ref(old_base_ref)
    if (old_base_ref['bytes'] != len(old_base_raw)
            or old_base_ref['sha256'] != hashlib.sha256(old_base_raw).hexdigest()):
        raise ValueError('old v3 base snapshot differs')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(old_base_raw)) as stream:
            expanded = stream.read(MAX_SIDECAR_BYTES + 1)
        if len(expanded) > MAX_SIDECAR_BYTES:
            raise ValueError('old sidecar snapshot expands beyond limit')
        old_sidecar = json.loads(expanded)
    except (OSError, ValueError) as error:
        raise ValueError('invalid old sidecar snapshot') from error
    fixed.validate_route_change(old_sidecar, new_sidecar)
    if ({key: value for key, value in old_sidecar.items() if key != 'f'}
            != {key: value for key, value in new_sidecar.items() if key != 'f'}):
        raise ValueError('non-route sidecar metadata changed')
    # No hypothesis about unrecognized catalog extensions or v3 overrides.
    allowed = {'version', 'kind', 'files', 'base_sidecar', 'generation', 'parent_generation', 'document_reset'}
    if set(catalog) != allowed:
        raise ValueError('v3 catalog fields need independent review')

    packed = gzip.compress(publication.encode(new_sidecar), compresslevel=9, mtime=0)
    packed_sha = hashlib.sha256(packed).hexdigest()
    base_ref = publication.metadata(fixed.ASSETS,
        f'reader-index/snapshots/{packed_sha}/reader_assets.json.gz', packed)
    publication.base_sidecar_ref(base_ref)
    body = {'version': 3, 'kind': 'reader-reading-catalog', 'files': copy.deepcopy(catalog['files']),
            'base_sidecar': base_ref, 'parent_generation': pointer['generation'],
            'document_reset': receipt['reset_id']}
    generation = publication.text.digest(body)
    proposed_catalog = {**body, 'generation': generation}
    publication.validate_catalog(proposed_catalog)
    catalog_raw = publication.encode(proposed_catalog)
    proposed_ref = publication.metadata(fixed.ASSETS,
        f'reader-index/v3/generations/{generation}/catalog.json', catalog_raw)
    proposed_pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': generation,
                        'catalog': proposed_ref, 'resources': [proposed_ref]}
    projection = publication.project_sidecar(new_sidecar, proposed_catalog)
    changed = {key for key in old_sidecar['f'].keys() | projection['f'].keys()
               if old_sidecar['f'].get(key) != projection['f'].get(key)}
    if changed != {fixed.SOURCE_KEY} or projection['f'].get(fixed.SOURCE_KEY) != fixed.route(fixed.ROOT):
        raise ValueError('projected routes differ outside the exact CHM')
    # Existing document GC insists its durable receipt names the *current*
    # generation and base, not merely an inherited reset id. This model never
    # rewrites or authorizes that receipt; the conflict remains a blocker.
    compatibility = (receipt['generation'] == generation and receipt['base_snapshot'] == base_ref)
    return {'pointer': proposed_pointer, 'catalog': proposed_catalog, 'projection': projection,
            'base_snapshot_sha256': packed_sha, 'current_reset_receipt_compatible': compatibility,
            'live_projection_verified': False, 'consumer_acceptance_proved': False,
            'recovery_authorized': False, 'gc_authorized': False}


def evaluate_fixture_receipts(simulation, fixtures):
    """Validate supplied synthetic snapshots, with no claim of real fetches."""
    if (not isinstance(simulation, dict) or not isinstance(fixtures, dict)
            or set(fixtures) != {'hf', 'pages'}):
        raise ValueError('both synthetic consumer fixtures are required')
    pointer, catalog, expected = simulation['pointer'], simulation['catalog'], simulation['projection']
    publication.validate_catalog(catalog)
    catalog_ref = publication.metadata(fixed.ASSETS,
        f"reader-index/v3/generations/{catalog['generation']}/catalog.json", publication.encode(catalog))
    if (pointer.get('version') != 1 or pointer.get('kind') != 'reader-reading-pointer'
            or pointer.get('generation') != catalog['generation'] or pointer.get('catalog') != catalog_ref
            or pointer.get('resources') != [catalog_ref] or not isinstance(expected, dict)
            or expected.get('v') != 1 or not isinstance(expected.get('f'), dict)
            or expected['f'].get(fixed.SOURCE_KEY) != fixed.route(fixed.ROOT)
            or expected.get('v3') != {'generation': catalog['generation'],
                                      'catalog_sha256': catalog_ref['sha256'], 'files': len(catalog['files'])}):
        raise ValueError('synthetic projection identity differs')
    for surface, fixture in fixtures.items():
        if not isinstance(fixture, dict) or set(fixture) != {'receipt', 'projection'}:
            raise ValueError('invalid synthetic consumer fixture')
        receipt, projection = fixture['receipt'], fixture['projection']
        if (not isinstance(receipt, dict) or receipt.get('version') != 1
                or receipt.get('surface') != surface or receipt.get('active') is not True
                or receipt.get('generation') != pointer['generation']
                or receipt.get('catalog_sha256') != pointer['catalog']['sha256']
                or receipt.get('files') != len(catalog['files'])):
            raise ValueError('synthetic consumer identity differs: ' + surface)
        if projection != expected:
            raise ValueError('synthetic consumer complete projection differs: ' + surface)
        if surface == 'pages':
            packed = gzip.compress(publication.encode(projection), compresslevel=9, mtime=0)
            if (receipt.get('reader_contract') != 'pdf-reading-v3-v1'
                    or receipt.get('sidecar_sha256') != hashlib.sha256(packed).hexdigest()):
                raise ValueError('synthetic Pages receipt differs from its projection')
    return {'synthetic_fixtures_match': True, 'live_projection_verified': False,
            'consumer_acceptance_proved': False, 'recovery_authorized': False, 'gc_authorized': False}
