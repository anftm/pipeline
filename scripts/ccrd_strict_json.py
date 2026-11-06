"""Reject ambiguous JSON for offline CCRD candidates; no storage or network."""
import json


def loads(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value

    def reject_constant(_value):
        raise ValueError('non-finite JSON number')

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
