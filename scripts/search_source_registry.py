"""Exact Search corpus admission; only these additions may bridge the old parent."""
import re

PREVIOUS_SOURCES = (
    'VoiceOfML/VOMEBOOK', 'VoiceOfML/SovMaterials', 'VoiceOfML/GPCREducation',
    'VoiceOfML/Teachers', 'VoiceOfML/MLMRL-Library', 'VoiceOfML/Japanese-Materials',
    'VoiceOfML/A-Historical-Learning-Data', 'VoiceOfML/MLMRL-Hub', 'VoiceOfML/Omnibus',
    'VoiceOfML/Marxists-Library',
)
ADDED_SOURCES = ('VoiceOfML/Scan-m', 'VoiceOfML/MZDBL-Library')
SOURCES = PREVIOUS_SOURCES + ADDED_SOURCES
REVISION = re.compile(r'[a-f0-9]{40}\Z')


def accepts_baseline(value, expected):
    if not isinstance(value, dict) or not all(isinstance(v,str) and REVISION.fullmatch(v) for v in value.values()):
        return False
    keys, wanted = set(value), set(expected)
    return keys == wanted or (wanted == set(SOURCES) and keys == set(PREVIOUS_SOURCES))
