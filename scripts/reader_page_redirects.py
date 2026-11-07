"""Versioned, narrowly scoped redirects for retired PDF derivative locations.

The mapping preserves exact legacy paths until the canonical indexes are
independently migrated. It does not prove that any object may be deleted.
"""
import copy
import re

try:
    from . import shared
except ImportError:
    import shared


FIELD = 'derived_pdf_redirects'
OLD = re.compile(r'derived/[A-Za-z0-9._-]+/[a-f0-9]{32}/document\.pdf\Z')
NEW = re.compile(r'objects/([a-f0-9]{2})/([a-f0-9]{64})/([a-f0-9]{16})/document\.pdf\Z')
BUCKET = shared.PDF_PAGES_BUCKET
PATH_FIELDS = (('p', 'b'), ('pd', 'pdb'), ('o', 'ob'), ('c', 'cb'), ('f', 'fb'))


def validated(catalog):
    redirects = catalog.get(FIELD, {})
    if not isinstance(redirects, dict) or len(redirects) > 10000:
        raise ValueError('invalid derived PDF redirects')
    targets = set()
    for old, new in redirects.items():
        match = NEW.fullmatch(new) if isinstance(new, str) else None
        if not isinstance(old, str) or not OLD.fullmatch(old) or not match or match.group(1) != match.group(2)[:2]:
            raise ValueError('invalid derived PDF redirect path')
        if new in targets:
            raise ValueError('multiple legacy PDFs cannot claim one redirect target')
        targets.add(new)
    return redirects


def rewrite(base, redirects):
    """Rewrite only exact bucket-qualified compact route paths, including aliases."""
    validated({FIELD: redirects})
    if not isinstance(base, dict) or base.get('v') != 1 or not isinstance(base.get('f'), dict):
        raise ValueError('invalid base sidecar')
    result = copy.deepcopy(base)
    changed = 0
    for entry in result['f'].values():
        if not isinstance(entry, dict):
            raise ValueError('invalid canonical sidecar entry')
        default = entry.get('b') or shared.READER_ASSETS_BUCKET
        touched = False
        for field, qualifier in PATH_FIELDS:
            value = entry.get(field)
            bucket = entry.get(qualifier) or default
            if isinstance(value, str) and value in redirects and bucket == BUCKET:
                entry[field] = redirects[value]
                touched = True
        changed += int(touched)
    # No exact old path may silently survive in an unknown field or metadata.
    # A cross-bucket collision is conservatively blocked as well.
    import json
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if any(old.encode() in encoded for old in redirects):
        raise ValueError('legacy PDF remains in an unsupported sidecar field')
    return result, changed


def preserve(previous, candidate):
    """Ordinary v3 publishers must never silently forget a live redirect."""
    old, new = validated(previous), validated(candidate)
    if any(new.get(path) != target for path, target in old.items()):
        raise ValueError('new catalog must preserve derived PDF redirects')
