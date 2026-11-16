"""Read completed document-reset receipts before considering another inventory."""
import re

try:
    from . import reader_document_reset as reset
except ImportError:
    import reader_document_reset as reset

BUCKETS = {
    'djvu-pdf': reset.PAGES,
    'document-pages': reset.PAGES,
    'document-control': reset.ASSETS,
    'ocr-inputs': reset.INPUTS,
    'ocr-library': 'vomebook/ccrd-index',
}


def completed(store, reset_id, scope):
    if scope not in BUCKETS or not re.fullmatch(r'[a-z0-9-]{8,80}', reset_id):
        raise ValueError('invalid completed reset identity')
    path = f'reader-index/gc/document-reset/{reset_id}/{scope}/collection.json'
    try:
        value = reset.decode(store.read_bytes(reset.ASSETS, path))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or type(value.get('complete')) is not bool:
        raise ValueError('reset completion state is invalid')
    if value['complete'] is False:
        return None
    if (value.get('kind') != 'reader-document-reset-collection' or value.get('version') != 1
            or value.get('applied') is not True or value.get('scope') != scope
            or value.get('reset_id') != reset_id or value.get('bucket') != BUCKETS[scope]
            or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('plan_sha256')))):
        raise ValueError('completed reset receipt identity differs')
    for key in ('planned_objects', 'accepted_objects', 'confirmed_deleted_objects',
                'planned_bytes', 'confirmed_deleted_bytes', 'remaining_objects', 'retained_objects'):
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError('completed reset counts invalid')
    if (value['planned_objects'] != value['accepted_objects']
            or value['planned_objects'] != value['confirmed_deleted_objects']
            or value['planned_bytes'] != value['confirmed_deleted_bytes']
            or value['remaining_objects'] < value['retained_objects']):
        raise ValueError('completed reset counts disagree')
    batches = value.get('accepted_batches')
    if (not isinstance(batches, list) or any(not isinstance(b, str) or not re.fullmatch(r'[0-9a-f]{64}', b) for b in batches)
            or len(set(batches)) != len(batches)
            or len(batches) != (value['planned_objects'] + 499) // 500):
        raise ValueError('completed reset batch receipt incomplete')
    return value
